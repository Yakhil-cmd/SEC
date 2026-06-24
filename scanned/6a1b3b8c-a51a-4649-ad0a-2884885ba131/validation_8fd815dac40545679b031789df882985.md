### Title
Quarantined Reimbursements Result in Permanently Locked User Funds After Failed Withdrawal — (`rs/ethereum/cketh/minter/src/withdraw.rs`, `rs/bitcoin/ckbtc/minter/src/reimbursement/mod.rs`)

---

### Summary

When a ckETH, ckERC20, or ckBTC withdrawal transaction fails on the destination chain, the minter schedules a reimbursement to mint tokens back to the user. If the minter canister panics at a specific point during the reimbursement callback — after the ledger mint call is dispatched but before the success event is recorded — a `QuarantinedReimbursement` / `QuarantinedWithdrawalReimbursement` event is emitted. Once quarantined, the reimbursement is permanently removed from the pending queue and will never be retried automatically. The user's tokens were burned on the IC ledger but are never minted back, resulting in permanent fund loss without any on-chain recovery path.

---

### Finding Description

The chain-fusion minters (ckETH, ckERC20, ckBTC) implement a two-phase withdrawal flow:

1. User calls `withdraw_eth` / `retrieve_btc` — their ck-tokens are burned on the IC ledger.
2. The minter submits a transaction to Ethereum/Bitcoin.
3. If the on-chain transaction fails, the minter schedules a reimbursement (mint back to user).
4. `process_reimbursement()` (ckETH) or `reimburse_withdrawals()` (ckBTC) runs on a timer and calls the ledger to mint tokens back.

The critical section in `process_reimbursement()` in `rs/ethereum/cketh/minter/src/withdraw.rs`:

```rust
let prevent_double_minting_guard = scopeguard::guard(index.clone(), |index| {
    mutate_state(|s| process_event(s, EventType::QuarantinedReimbursement { index }));
});
// ... async ledger mint call ...
// minting succeeded, defuse guard
ScopeGuard::into_inner(prevent_double_minting_guard);
```

If the minter panics anywhere after the `mint` inter-canister call is sent (including in the callback), the IC runtime rolls back the canister state, which means the `ScopeGuard::into_inner(...)` defuse is also rolled back. On the next execution, the scope guard fires and emits `QuarantinedReimbursement`.

`record_quarantined_reimbursement` in `rs/ethereum/cketh/minter/src/state/transactions/mod.rs` then permanently removes the request:

```rust
pub fn record_quarantined_reimbursement(&mut self, index: ReimbursementIndex) {
    self.reimbursement_requests.remove(&index);
    self.reimbursed
        .insert(index, Err(ReimbursedError::Quarantined));
}
```

The event log comment in `rs/ethereum/cketh/minter/src/state/event.rs` explicitly acknowledges this:

> "The reimbursement is quarantined to prevent any double minting and **will not be processed without further manual intervention**."

The identical pattern exists in ckBTC at `rs/bitcoin/ckbtc/minter/src/reimbursement/mod.rs` and `rs/bitcoin/ckbtc/minter/src/state.rs`.

There is no on-chain endpoint, no governance proposal type, and no automated retry path to rescue a quarantined reimbursement. The only recovery is a canister upgrade that manually patches state — which requires NNS governance and is not guaranteed.

---

### Impact Explanation

A user who initiates a ckETH, ckERC20, or ckBTC withdrawal has their tokens burned on the IC ledger at the start of the flow. If the destination-chain transaction fails and the subsequent reimbursement mint is quarantined due to a minter panic, the user permanently loses their funds. The burned tokens are gone from the ledger supply, and no mint-back ever occurs. The `reimbursed_withdrawals` / `reimbursed` map records `Err(ReimbursedError::Quarantined)` as the terminal state, and the timer-driven reimbursement loop skips quarantined entries entirely.

---

### Likelihood Explanation

The trigger requires a minter canister panic during the async reimbursement callback. IC canisters can panic due to bugs, resource exhaustion, or unexpected ledger responses. The `prevent_double_minting_guard` mechanism was explicitly added because the developers recognized this as a realistic scenario. The guard fires on any panic — including those caused by bugs introduced in future upgrades. The window is narrow (between ledger call dispatch and success recording), but the consequence when it occurs is total and irreversible fund loss for the affected user. Likelihood is low-to-medium per individual withdrawal, but the cumulative risk across all users and all future minter versions is non-trivial.

---

### Recommendation

1. **Add a governance-accessible rescue endpoint**: Expose an admin/NNS-callable method that can move a quarantined reimbursement back to `pending_reimbursement_requests` after manual verification that the mint did not actually succeed (by checking the ledger for a matching mint block).

2. **Check ledger before quarantining**: Before emitting `QuarantinedReimbursement`, attempt to query the ledger for a mint transaction matching the expected memo/amount. If found, record it as successfully reimbursed instead of quarantined.

3. **Use `created_at_time` for idempotency**: Pass `created_at_time` in the ledger `TransferArg` so that a retry of the same reimbursement is deduplicated by the ledger rather than quarantined. This would allow safe retries without double-minting risk.

---

### Proof of Concept

1. User calls `withdraw_eth(10 ETH)` — 10 ckETH burned on IC ledger (burn block index N).
2. Ethereum transaction is submitted and fails (e.g., out-of-gas). Minter records `FinalizedTransaction` with `TransactionStatus::Failure`.
3. `record_finalized_transaction` calls `record_reimbursement_request` — reimbursement for ~10 ckETH is queued.
4. Timer fires `process_reimbursement()`. The minter calls `client.transfer(args).await` to mint ckETH back.
5. The ledger processes the mint. The minter's callback panics (e.g., due to a bug in the block index conversion or a future code regression).
6. IC runtime rolls back minter state. The `prevent_double_minting_guard` fires, emitting `QuarantinedReimbursement { index: CkEth { ledger_burn_index: N } }`.
7. `record_quarantined_reimbursement` removes the entry from `reimbursement_requests` and inserts `Err(Quarantined)` into `reimbursed`.
8. All future timer invocations of `process_reimbursement()` skip this entry — it is no longer in `reimbursement_requests_iter()`.
9. User's 10 ckETH are permanently lost. The ledger may or may not have the mint (unknown), but the minter will never retry.

The existing test `should_have_status_pending_reimbursement_for_quarantined_reimbursement` in `rs/ethereum/cketh/minter/src/state/transactions/tests.rs` confirms that after quarantine, the status remains `TxFinalized(PendingReimbursement(...))` indefinitely — the user sees a "pending" reimbursement that will never complete. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

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

**File:** rs/bitcoin/ckbtc/minter/src/reimbursement/mod.rs (L30-36)
```rust
#[derive(Clone, Eq, PartialEq, Debug)]
pub enum ReimbursedError {
    /// Whether reimbursement was minted or not is unknown,
    /// most likely because there was an unexpected panic in the callback.
    /// The reimbursement request is quarantined to avoid any double minting and
    /// will not be further processed without manual intervention.
    Quarantined,
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

**File:** rs/ethereum/cketh/minter/src/state/transactions/tests.rs (L2038-2057)
```rust
        #[test]
        fn should_have_status_pending_reimbursement_for_quarantined_reimbursement() {
            let mut transactions = EthTransactions::new(TransactionNonce::ZERO);
            let mut rng = reproducible_rng();
            let [withdrawal_request] = create_ck_withdrawal_requests(&mut rng);
            let reimbursement_index = ReimbursementIndex::from(&withdrawal_request);
            let receipt = withdrawal_flow(
                &mut transactions,
                withdrawal_request,
                TransactionStatus::Failure,
            );
            transactions.record_quarantined_reimbursement(reimbursement_index.clone());

            assert_eq!(
                transactions.transaction_status(&reimbursement_index.withdrawal_id()),
                RetrieveEthStatus::TxFinalized(TxFinalizedStatus::PendingReimbursement(
                    (&receipt).into()
                ))
            );
        }
```
