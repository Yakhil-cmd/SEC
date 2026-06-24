### Title
Quarantined Reimbursement Permanently Locks User Funds After Failed ckETH/ckERC20 Withdrawal - (`rs/ethereum/cketh/minter/src/withdraw.rs`)

---

### Summary

The ckETH/ckERC20 minter's `process_reimbursement()` function uses a panic-triggered scope guard to prevent double-minting. If the minter panics after contacting the ledger to mint reimbursement tokens but before defusing the guard, the reimbursement is permanently quarantined via `EventType::QuarantinedReimbursement`. The quarantined state is terminal — the user's burned ckETH/ckERC20 tokens from a failed Ethereum withdrawal are permanently unrecoverable without a canister upgrade and manual operator intervention.

---

### Finding Description

When a ckETH or ckERC20 Ethereum withdrawal transaction fails on-chain, the minter schedules a reimbursement: it must mint back the burned ckETH (gas fee) and/or burned ckERC20 tokens to the user. This is the IC analog of Taiko's `onMessageRecalled()` path.

In `process_reimbursement()`:

```rust
let prevent_double_minting_guard = scopeguard::guard(index.clone(), |index| {
    mutate_state(|s| process_event(s, EventType::QuarantinedReimbursement { index }));
});
// ...
let block_index = match client.transfer(args).await { ... };
// ...
ScopeGuard::into_inner(prevent_double_minting_guard); // defuse on success
```

If a panic occurs anywhere in the async callback **after** `client.transfer(args).await` is called but **before** `ScopeGuard::into_inner(prevent_double_minting_guard)` is reached, the scope guard fires and records `EventType::QuarantinedReimbursement`. This transitions the reimbursement into `record_quarantined_reimbursement()`:

```rust
pub fn record_quarantined_reimbursement(&mut self, index: ReimbursementIndex) {
    self.reimbursement_requests.remove(&index);
    self.reimbursed.insert(index, Err(ReimbursedError::Quarantined));
}
```

The `ReimbursedError::Quarantined` state is **terminal**. The reimbursement is removed from `reimbursement_requests` (the retry queue) and placed into `reimbursed` as an error. The timer-driven `process_reimbursement()` only iterates over `reimbursement_requests_iter()`, so quarantined entries are **never retried automatically**.

The same pattern exists in the ckBTC minter's `reimburse_withdrawals()`.

---

### Impact Explanation

A user who burned ckETH and/or ckERC20 tokens to initiate a withdrawal, whose Ethereum transaction then failed, and whose reimbursement processing subsequently panicked, permanently loses their burned tokens. The `ReimbursedError::Quarantined` state is explicitly documented as requiring "manual intervention" — meaning a canister upgrade is the only recovery path. Without such intervention, the tokens are permanently locked in the minter's accounting with no on-chain recovery mechanism available to the user.

This is the direct IC analog of the Taiko M-04 finding: a "recall" (reimbursement) path that can result in permanent fund lock with no user-accessible recovery.

---

### Likelihood Explanation

A panic in the async callback window is not directly attacker-controlled, but it is a realistic operational risk:

1. The `block_index.0.to_u64().expect("block index should fit into u64")` call on line 98 will **panic** if the ledger returns a block index that does not fit into `u64`. A ledger with a sufficiently large block index (e.g., after very high transaction volume) would trigger this deterministically for every reimbursement attempt, permanently quarantining all pending reimbursements.
2. Any future code change introducing a panic between the `await` and the `ScopeGuard::into_inner` call would trigger the same outcome.
3. The ckBTC minter's `reimburse_withdrawals()` has the identical structural risk.

The `u64` overflow path (item 1) is a concrete, reachable trigger that does not require a privileged role.

---

### Recommendation

1. Replace `.expect("block index should fit into u64")` with a graceful error path that defuses the guard and retries, rather than panicking and triggering quarantine.
2. Add an operator-callable endpoint to un-quarantine a reimbursement (with appropriate access control) so recovery does not require a full canister upgrade.
3. Consider logging a high-priority alert when a reimbursement is quarantined so operators are immediately notified.

---

### Proof of Concept

**Trigger path for the `u64` overflow panic:**

1. The ckETH/ckERC20 ledger accumulates more than `u64::MAX` (≈ 1.8 × 10¹⁹) transactions (or a bug causes an abnormally large block index to be returned).
2. A user initiates a ckERC20 withdrawal; the Ethereum transaction fails; a reimbursement is scheduled.
3. `process_reimbursement()` calls `client.transfer(args).await` → ledger returns `Ok(Ok(large_block_index))`.
4. `.to_u64().expect("block index should fit into u64")` panics.
5. The scope guard fires → `EventType::QuarantinedReimbursement` is recorded.
6. `record_quarantined_reimbursement()` removes the entry from `reimbursement_requests` and inserts `Err(ReimbursedError::Quarantined)` into `reimbursed`.
7. The timer never retries this entry. The user's burned ckETH and ckERC20 tokens are permanently lost without a canister upgrade.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L272-279)
```rust
    /// Whether reimbursement was minted or not is unknown,
    /// most likely because there was an unexpected panic in the callback.
    /// The reimbursement request is quarantined to avoid any double minting and
    /// will not be further processed without manual intervention.
    Quarantined,
}

struct DisplayOption<'a, T>(&'a Option<T>);
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

**File:** rs/bitcoin/ckbtc/minter/src/reimbursement/mod.rs (L64-107)
```rust
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
```
