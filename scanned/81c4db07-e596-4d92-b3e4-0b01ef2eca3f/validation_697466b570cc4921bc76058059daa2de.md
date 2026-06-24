### Title
Permanent Token Loss via Quarantined Reimbursement — No Recovery Path After Panic in Callback (`rs/ethereum/cketh/minter/src/withdraw.rs`, `rs/bitcoin/ckbtc/minter/src/reimbursement/mod.rs`)

---

### Summary

Both the ckETH minter and the ckBTC minter implement a "quarantine" mechanism to prevent double-minting when an unexpected panic occurs during a reimbursement callback. Once a reimbursement is quarantined, the tokens that were already burned (on withdrawal) are **permanently lost** — there is no on-chain recovery path, no admin endpoint, and no automated retry. This is the direct IC analog of the "bad debt with no write-off mechanism" described in the reference report.

---

### Finding Description

**ckETH/ckERC20 minter path** (`rs/ethereum/cketh/minter/src/withdraw.rs`):

In `process_reimbursement()`, a `scopeguard` is armed before the async `client.transfer(args).await` call:

```rust
let prevent_double_minting_guard = scopeguard::guard(index.clone(), |index| {
    mutate_state(|s| process_event(s, EventType::QuarantinedReimbursement { index }));
});
```

If the minter panics **after** the ledger mint succeeds but **before** `ScopeGuard::into_inner(prevent_double_minting_guard)` is called (i.e., before the guard is defused), the IC state rolls back the `mutate_state` that recorded the successful mint, but the guard fires and records `QuarantinedReimbursement`. The reimbursement is then moved from `reimbursement_requests` to `reimbursed` with `Err(ReimbursedError::Quarantined)`:

```rust
pub fn record_quarantined_reimbursement(&mut self, index: ReimbursementIndex) {
    self.reimbursement_requests.remove(&index);
    self.reimbursed.insert(index, Err(ReimbursedError::Quarantined));
}
```

Once in `Err(ReimbursedError::Quarantined)`, the entry is **never retried** — `process_reimbursement()` only iterates `reimbursement_requests_iter()`, which skips entries already in `reimbursed`. There is no public endpoint to un-quarantine or force-retry a reimbursement. The user's tokens (ckETH or ckERC20) that were burned on withdrawal are permanently destroyed.

**ckBTC minter path** (`rs/bitcoin/ckbtc/minter/src/reimbursement/mod.rs`):

The same pattern exists in `reimburse_withdrawals()`:

```rust
let prevent_double_minting_guard = scopeguard::guard(burn_index, |index| {
    state::mutate_state(|s| {
        state::audit::quarantine_withdrawal_reimbursement(s, index, runtime)
    });
});
```

After quarantine, `quarantine_withdrawal_reimbursement()` removes the entry from `pending_withdrawal_reimbursements` and inserts `Err(ReimbursedError::Quarantined)` into `reimbursed_withdrawals`. The status shown to the user becomes `RetrieveBtcStatusV2::Unknown` — the ckBTC was burned, the BTC was never sent, and the user receives nothing back.

---

### Impact Explanation

- **Ledger conservation break**: Tokens are burned on the ledger (reducing `total_supply`) but the corresponding mint-back never occurs. The ckETH/ckBTC supply is permanently deflated relative to the backing assets held by the minter.
- **Permanent user fund loss**: The user who initiated the withdrawal loses their tokens with no recourse. The minter holds the backing asset (ETH/BTC) but has no mechanism to release it.
- **No recovery without governance upgrade**: The only way to recover is a NNS governance proposal to upgrade the minter canister with a manual fix — there is no on-chain self-service recovery path.
- **Scope**: Affects all ckETH, ckERC20, and ckBTC withdrawals that fail at the reimbursement stage due to a panic in the async callback window.

---

### Likelihood Explanation

The panic window is narrow but real:

1. The minter calls `client.transfer(args).await` (a cross-canister call to the ledger).
2. The ledger mints successfully and returns `Ok(Ok(block_index))`.
3. Between the return and `ScopeGuard::into_inner(...)`, any trap/panic (e.g., from `block_index.0.to_u64().expect(...)` overflowing, or from `mutate_state` panicking on an invariant assertion) causes the guard to fire.
4. The IC rolls back the `mutate_state` recording the successful mint, but the guard's `mutate_state` fires in the cleanup path and records `Quarantined`.

The `block_index.0.to_u64().expect("block index should fit into u64")` at line 98 of `withdraw.rs` is a concrete panic point reachable if the ledger ever returns a block index ≥ 2^64. This is attacker-reachable if a malicious or buggy ledger canister is substituted (e.g., for a ckERC20 token whose ledger is controlled by an SNS or third party).

---

### Recommendation

1. **Add a `created_at_time` to the reimbursement transfer** so that if the minter re-attempts after a quarantine, the ledger deduplicates and returns the existing block index rather than minting again. This makes safe retry possible.
2. **Replace the `expect()` panic** at `block_index.0.to_u64().expect(...)` with a graceful error that defuses the guard and retries later, rather than triggering quarantine.
3. **Add an admin/governance endpoint** to un-quarantine a specific reimbursement index and re-queue it for processing, with appropriate safeguards against double-minting (e.g., using `created_at_time` deduplication).
4. **Emit a metric** for quarantined reimbursements so monitoring can detect and alert on this condition.

---

### Proof of Concept

**Entry path (ckETH)**:
1. User calls `withdraw_eth` → ckETH is burned on the ledger.
2. Ethereum transaction fails → `record_finalized_transaction` queues a `ReimbursementRequest`.
3. Timer fires `process_reimbursement()`.
4. Minter calls `client.transfer(args).await` → ledger mints ckETH back to user (success).
5. Minter executes `block_index.0.to_u64().expect("block index should fit into u64")` — if ledger returns `Nat` ≥ 2^64, this panics.
6. IC rolls back `mutate_state(|s| process_event(s, event))` (the success recording).
7. `prevent_double_minting_guard` fires → `QuarantinedReimbursement` is recorded.
8. `reimbursement_requests` entry is removed; `reimbursed` entry is `Err(Quarantined)`.
9. `process_reimbursement()` never processes this index again.
10. User's ckETH is permanently lost; the minter holds the ETH backing.

**Relevant code locations**: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L67-116)
```rust
    for (index, reimbursement_request) in reimbursements {
        // Ensure that even if we were to panic in the callback, after having contacted the ledger to mint the tokens,
        // this reimbursement request will not be processed again.
        let prevent_double_minting_guard = scopeguard::guard(index.clone(), |index| {
            mutate_state(|s| process_event(s, EventType::QuarantinedReimbursement { index }));
        });
        let ledger_canister_id = match index {
            ReimbursementIndex::CkEth { .. } => read_state(|s| s.cketh_ledger_id),
            ReimbursementIndex::CkErc20 { ledger_id, .. } => ledger_id,
        };
        let client = ICRC1Client {
            runtime: CdkRuntime,
            ledger_canister_id,
        };
        let memo = Memo::from(reimbursement_request.clone());
        let args = TransferArg {
            from_subaccount: None,
            to: Account {
                owner: reimbursement_request.to,
                subaccount: reimbursement_request
                    .to_subaccount
                    .map(LedgerSubaccount::to_bytes),
            },
            fee: None,
            created_at_time: None,
            memo: Some(memo),
            amount: Nat::from(reimbursement_request.reimbursed_amount),
        };
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

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L270-279)
```rust
#[derive(Clone, Eq, PartialEq, Debug)]
pub enum ReimbursedError {
    /// Whether reimbursement was minted or not is unknown,
    /// most likely because there was an unexpected panic in the callback.
    /// The reimbursement request is quarantined to avoid any double minting and
    /// will not be further processed without manual intervention.
    Quarantined,
}

struct DisplayOption<'a, T>(&'a Option<T>);
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L772-779)
```rust
    /// Quarantine the reimbursement request identified by its index to prevent double minting.
    /// WARNING!: It's crucial that this method does not panic,
    /// since it's called inside the clean-up callback, when an unexpected panic did occur before.
    pub fn record_quarantined_reimbursement(&mut self, index: ReimbursementIndex) {
        self.reimbursement_requests.remove(&index);
        self.reimbursed
            .insert(index, Err(ReimbursedError::Quarantined));
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/reimbursement/mod.rs (L57-116)
```rust
/// Reimburse withdrawals that were canceled.
pub async fn reimburse_withdrawals<R: CanisterRuntime>(runtime: &R) {
    if state::read_state(|s| s.pending_withdrawal_reimbursements.is_empty()) {
        return;
    }
    let pending_reimbursements = state::read_state(|s| s.pending_withdrawal_reimbursements.clone());
    let mut error_count = 0;
    for (burn_index, reimbursement) in pending_reimbursements {
        // Ensure that even if we were to panic in the callback, after having contacted the ledger to mint the tokens,
        // this reimbursement request will not be processed again.
        let prevent_double_minting_guard = scopeguard::guard(burn_index, |index| {
            state::mutate_state(|s| {
                state::audit::quarantine_withdrawal_reimbursement(s, index, runtime)
            });
        });
        let memo = MintMemo::ReimburseWithdrawal {
            withdrawal_id: burn_index,
        };
        match runtime
            .mint_ckbtc(
                reimbursement.amount,
                reimbursement.account,
                Memo::from(crate::memo::encode(&memo)),
            )
            .await
        {
            Ok(mint_index) => {
                log!(
                    Priority::Debug,
                    "[reimburse_withdrawals]: Successfully reimbursed {:?} at mint block index {}",
                    reimbursement,
                    mint_index
                );
                state::mutate_state(|s| {
                    state::audit::reimburse_withdrawal_completed(s, burn_index, mint_index, runtime)
                });
            }
            Err(err) => {
                log!(
                    Priority::Info,
                    "[reimburse_withdrawals]: Failed to reimburse {:?}: {:?}. Will retry later",
                    reimbursement,
                    err
                );
                error_count += 1;
            }
        }
        // Defuse the guard. Note that in case of a panic in the callback (either before or after this point)
        // the defuse will not be effective (due to state rollback), and the guard that was
        // setup before the `mint_ckbtc` async call will be invoked.
        scopeguard::ScopeGuard::into_inner(prevent_double_minting_guard);
    }

    if error_count > 0 {
        log!(
            Priority::Info,
            "[reimburse_withdrawals] Failed to reimburse {error_count} withdrawal requests, retrying later."
        );
    }
}
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L1770-1777)
```rust
    /// Quarantine the reimbursement request identified by its index to prevent double minting.
    /// WARNING!: It's crucial that this method does not panic,
    /// since it's called inside the clean-up callback, when an unexpected panic did occur before.
    pub fn quarantine_withdrawal_reimbursement(&mut self, burn_index: LedgerBurnIndex) {
        self.pending_withdrawal_reimbursements.remove(&burn_index);
        self.reimbursed_withdrawals
            .insert(burn_index, Err(ReimbursedError::Quarantined));
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
