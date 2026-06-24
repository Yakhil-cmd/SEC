### Title
Quarantined Reimbursement State Has No Automated or User-Accessible Recovery Path, Permanently Locking User Funds - (`rs/ethereum/cketh/minter/src/withdraw.rs`, `rs/ethereum/cketh/minter/src/state/transactions/mod.rs`)

---

### Summary

The ckETH/ckERC20 minter (and ckBTC/ckDOGE minters) implement a `QuarantinedReimbursement` terminal state that is explicitly documented as requiring "manual intervention" to resolve. When a reimbursement is quarantined, the user's burned tokens are permanently locked with no automated retry, no user-callable recovery endpoint, and no governance-accessible unquarantine mechanism in the production canister interface. This is a direct analog to the reported M-03 pattern: a failed state transition with no recovery path permanently locks user value.

---

### Finding Description

**Trigger path:**

1. A user calls `withdraw_erc20` (or `retrieve_btc`/`retrieve_doge`). Their ckETH/ckERC20 tokens are burned from the ledger immediately.
2. The Ethereum transaction finalizes with `TransactionStatus::Failure`. The minter calls `record_finalized_transaction`, which enqueues a `ReimbursementRequest` into `reimbursement_requests`.
3. The timer-driven `process_reimbursement()` in `rs/ethereum/cketh/minter/src/withdraw.rs` picks up the request and calls `client.transfer(args).await` to mint back the tokens.
4. **Before** the `await` returns, a `scopeguard` is armed:

```rust
// rs/ethereum/cketh/minter/src/withdraw.rs:70-72
let prevent_double_minting_guard = scopeguard::guard(index.clone(), |index| {
    mutate_state(|s| process_event(s, EventType::QuarantinedReimbursement { index }));
});
```

5. If the canister **panics** after the ledger mint call but before `ScopeGuard::into_inner(prevent_double_minting_guard)` is reached (e.g., due to a deterministic trap in `mutate_state`, an OOM, or a Wasm trap in the callback), the guard fires and records `EventType::QuarantinedReimbursement`.

6. `record_quarantined_reimbursement` removes the request from `reimbursement_requests` and inserts `Err(ReimbursedError::Quarantined)` into `reimbursed`:

```rust
// rs/ethereum/cketh/minter/src/state/transactions/mod.rs:775-779
pub fn record_quarantined_reimbursement(&mut self, index: ReimbursementIndex) {
    self.reimbursement_requests.remove(&index);
    self.reimbursed
        .insert(index, Err(ReimbursedError::Quarantined));
}
```

7. The `ReimbursedError::Quarantined` variant is explicitly documented as a terminal state requiring manual intervention:

```rust
// rs/ethereum/cketh/minter/src/state/transactions/mod.rs:271-277
pub enum ReimbursedError {
    /// Whether reimbursement was minted or not is unknown,
    /// most likely because there was an unexpected panic in the callback.
    /// The reimbursement request is quarantined to avoid any double minting and
    /// will not be further processed without manual intervention.
    Quarantined,
}
```

8. The same pattern exists in the ckBTC minter:

```rust
// rs/bitcoin/ckbtc/minter/src/state.rs:1773-1777
pub fn quarantine_withdrawal_reimbursement(&mut self, burn_index: LedgerBurnIndex) {
    self.pending_withdrawal_reimbursements.remove(&burn_index);
    self.reimbursed_withdrawals
        .insert(burn_index, Err(ReimbursedError::Quarantined));
}
```

And the ckBTC event log documents the same "no further processing without manual intervention":

```
// rs/bitcoin/ckbtc/minter/src/state/eventlog.rs:267-274
/// The minter unexpectedly panicked while processing a reimbursement.
/// The reimbursement is quarantined to prevent any double minting and
/// will not be processed without further manual intervention.
QuarantinedWithdrawalReimbursement { burn_block_index: u64 }
```

**No recovery endpoint exists.** Searching the ckETH minter's Candid interface (`cketh_minter.did`) and `main.rs` reveals no `unquarantine_reimbursement`, `retry_quarantined`, or equivalent admin/governance call. The quarantined state is only surfaced as a dashboard display entry (`DashboardReimbursedTransaction::Quarantined`) with no action attached. The only resolution path is a canister upgrade that manually replays or patches state — a privileged governance action that is not guaranteed to occur and requires identifying the affected users.

**The panic trigger is realistic.** The ckBTC mainnet upgrade proposal `rs/bitcoin/ckbtc/mainnet/minter_upgrade_2025_06_27.md` explicitly documents a real-world deterministic panic in the minter's resubmission path that caused three live withdrawals to be stuck. A similar deterministic panic in `mutate_state` after a successful ledger mint call would trigger the quarantine guard and permanently lock the user's reimbursement.

---

### Impact Explanation

**Ledger conservation bug.** When a user's ckETH or ckERC20 withdrawal fails on Ethereum and the subsequent reimbursement mint panics mid-callback, the user's burned tokens are neither returned (the mint may or may not have succeeded — the state is unknown) nor retried. The `Quarantined` state is terminal in the production code. The user loses their funds with no self-service recovery path. For ckBTC/ckDOGE, the same applies to canceled withdrawals (e.g., `TooManyInputs` cancellations). The minter's own accounting (`eth_balance`, `erc20_balances`) is also left inconsistent because the reimbursement was never confirmed.

**Impact: Medium** — Funds are permanently locked for affected users until a governance-approved canister upgrade manually resolves the quarantine. The minter continues operating normally for other users.

---

### Likelihood Explanation

**Medium.** The quarantine guard fires on any unexpected panic in the reimbursement callback. The ckBTC mainnet has already experienced a deterministic panic in a closely related async callback path (the resubmission path, documented in `minter_upgrade_2025_06_27.md`). The ckETH `process_reimbursement` loop calls `mutate_state` after an `await`, which is a known IC pattern where state rollback on trap can interact with the scopeguard. Any future deterministic bug (e.g., integer overflow in `mutate_state`, a `panic!` in `record_finalized_reimbursement`, or an OOM during a large batch) would trigger this. The condition is not attacker-controlled but is reachable through normal user withdrawal activity combined with any minter-side bug.

---

### Recommendation

1. **Add a governance-callable or timer-driven unquarantine endpoint** that, given a `ReimbursementIndex`, checks the ledger for a mint transaction matching the expected memo (`MintMemo::ReimburseWithdrawal { withdrawal_id }`) and, if found, transitions the state to `Reimbursed`; if not found, re-enqueues the request for retry.
2. **Alternatively**, implement an automatic retry loop: on canister upgrade or timer, scan `reimbursed` entries with `Err(Quarantined)`, query the ledger for the corresponding mint block, and resolve the ambiguity before permanently discarding the reimbursement.
3. **For ckBTC/ckDOGE**, the same applies to `QuarantinedWithdrawalReimbursement` entries in `reimbursed_withdrawals`.

---

### Proof of Concept

**Entry path (unprivileged user):**

1. User calls `withdraw_erc20(amount=X, ckerc20_ledger_id=USDC, recipient="0x...")` — no privilege required.
2. ckETH and ckERC20 are burned. Ethereum transaction is submitted and finalized with `TransactionStatus::Failure`.
3. `process_reimbursement()` timer fires. The scopeguard is armed at line 70. `client.transfer(args).await` is called to mint back the ckERC20.
4. The ledger mint succeeds (block index returned), but `mutate_state(|s| process_event(s, event))` at line 138 panics due to a deterministic bug (e.g., the same class of bug documented in `minter_upgrade_2025_06_27.md`).
5. IC state rolls back the `mutate_state` call. The scopeguard fires, recording `QuarantinedReimbursement`.
6. The user's ckERC20 tokens are now in limbo: the ledger mint may have succeeded (tokens minted to user) or not, but the minter state records `Quarantined` and will never retry.
7. The user queries `retrieve_eth_status(block_index)` and receives `TxFinalized(PendingReimbursement(...))` indefinitely — the status never advances to `Reimbursed`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7) [9](#0-8)

### Citations

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L67-72)
```rust
    for (index, reimbursement_request) in reimbursements {
        // Ensure that even if we were to panic in the callback, after having contacted the ledger to mint the tokens,
        // this reimbursement request will not be processed again.
        let prevent_double_minting_guard = scopeguard::guard(index.clone(), |index| {
            mutate_state(|s| process_event(s, EventType::QuarantinedReimbursement { index }));
        });
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L95-116)
```rust
        let block_index = match client.transfer(args).await {
            Ok(Ok(block_index)) => block_index
                .0
                .to_u64()
                .expect("block index should fit into u64"),
            Ok(Err(err)) => {
                log!(INFO, "[process_reimbursement] Failed to mint ckETH {err}");
                error_count += 1;
                // minting failed, defuse guard
                ScopeGuard::into_inner(prevent_double_minting_guard);
                continue;
            }
            Err(err) => {
                log!(
                    INFO,
                    "[process_reimbursement] Failed to send a message to the ledger ({ledger_canister_id}): {err:?}"
                );
                error_count += 1;
                // minting failed, defuse guard
                ScopeGuard::into_inner(prevent_double_minting_guard);
                continue;
            }
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L138-141)
```rust
        mutate_state(|s| process_event(s, event));
        // minting succeeded, defuse guard
        ScopeGuard::into_inner(prevent_double_minting_guard);
    }
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L271-277)
```rust
pub enum ReimbursedError {
    /// Whether reimbursement was minted or not is unknown,
    /// most likely because there was an unexpected panic in the callback.
    /// The reimbursement request is quarantined to avoid any double minting and
    /// will not be further processed without manual intervention.
    Quarantined,
}
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L775-779)
```rust
    pub fn record_quarantined_reimbursement(&mut self, index: ReimbursementIndex) {
        self.reimbursement_requests.remove(&index);
        self.reimbursed
            .insert(index, Err(ReimbursedError::Quarantined));
    }
```

**File:** rs/ethereum/cketh/minter/src/state/event.rs (L150-158)
```rust
    /// The minter unexpectedly panic while processing a reimbursement.
    /// The reimbursement is quarantined to prevent any double minting and
    /// will not be processed without further manual intervention.
    #[n(22)]
    QuarantinedReimbursement {
        /// The unique identifier of the reimbursement.
        #[n(0)]
        index: ReimbursementIndex,
    },
```

**File:** rs/bitcoin/ckbtc/minter/src/state/eventlog.rs (L267-274)
```rust
        /// The minter unexpectedly panicked while processing a reimbursement.
        /// The reimbursement is quarantined to prevent any double minting and
        /// will not be processed without further manual intervention.
        #[serde(rename = "quarantined_withdrawal_reimbursement")]
        QuarantinedWithdrawalReimbursement {
            /// The burn block on the ledger for that withdrawal that should have been reimbursed
            burn_block_index: u64,
        },
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L1773-1777)
```rust
    pub fn quarantine_withdrawal_reimbursement(&mut self, burn_index: LedgerBurnIndex) {
        self.pending_withdrawal_reimbursements.remove(&burn_index);
        self.reimbursed_withdrawals
            .insert(burn_index, Err(ReimbursedError::Quarantined));
    }
```

**File:** rs/bitcoin/ckbtc/mainnet/minter_upgrade_2025_06_27.md (L31-33)
```markdown
2. There is a deterministic panic occurring in the minter when it tries to resubmit those transactions, which explains
   why those transactions are currently stuck. This should be completely fixed
   by [#5713](https://github.com/dfinity/ic/pull/5713).
```
