Audit Report

## Title
ckETH Withdrawal Request Permanently Stuck in Pending Queue When Gas Fees Exceed Withdrawal Amount — (`File: rs/ethereum/cketh/minter/src/withdraw.rs`)

## Summary
When a ckETH withdrawal is accepted and the corresponding ckETH is burned on the IC ledger, a subsequent gas fee spike can cause `create_transaction` to return `CreateTransactionError::InsufficientTransactionFee`. The handler in `create_transactions_batch` responds exclusively by calling `reschedule_withdrawal_request`, which moves the request to the back of the pending queue with no retry limit, no reimbursement, and no cancellation. The user's ckETH is permanently burned with no corresponding ETH transfer and no recovery path.

## Finding Description
**Ingress check** (`rs/ethereum/cketh/minter/src/main.rs`, lines 291–296): `withdraw_eth` validates only that `amount >= cketh_minimum_withdrawal_amount`, a static governance-set value. It does not check against real-time gas fees.

**Burn path** (`rs/ethereum/cketh/minter/src/main.rs`, lines 301–336): After passing the check, ckETH is burned on the ledger and the withdrawal request is queued via `EventType::AcceptedEthWithdrawalRequest`. There is no rollback if the request later cannot be processed.

**Transaction creation failure** (`rs/ethereum/cketh/minter/src/state/transactions/mod.rs`, lines 1125–1133): In `create_transaction`, if `withdrawal_amount.checked_sub(max_transaction_fee)` returns `None` (fee exceeds amount), `CreateTransactionError::InsufficientTransactionFee` is returned.

**Silent reschedule loop** (`rs/ethereum/cketh/minter/src/withdraw.rs`, lines 281–291): The only handler for this error in `create_transactions_batch` is:
```rust
mutate_state(|s| s.eth_transactions.reschedule_withdrawal_request(request));
```
No reimbursement is queued, no finalization occurs, no retry counter is incremented.

**`reschedule_withdrawal_request`** (`rs/ethereum/cketh/minter/src/state/transactions/mod.rs`, lines 469–483): Simply removes the request and re-appends it to `pending_withdrawal_requests` with no bound on how many times this can happen.

**`process_reimbursement`** (`rs/ethereum/cketh/minter/src/withdraw.rs`, lines 46–148): The existing reimbursement infrastructure handles `ReimbursementIndex::CkEth`, but it is only triggered when a finalized transaction fails on-chain. It is never triggered for the `InsufficientTransactionFee` path, which never produces a finalized transaction at all.

**Contrast with ckBTC** (`rs/bitcoin/ckbtc/minter/src/lib.rs`, lines 412–434): The ckBTC minter handles `BuildTxError::AmountTooLow` by calling `remove_retrieve_btc_request` with `FinalizedStatus::AmountTooLow`, permanently removing the request from the queue. No equivalent finalization exists in the ckETH minter for this case.

## Impact Explanation
This is a **High** severity finding matching the allowed impact: *"Significant Chain Fusion, ck-token, ledger … security impact with concrete user or protocol harm."* A user's ckETH is irreversibly burned on the IC ledger (reducing their balance to zero for that amount), but the corresponding ETH is never sent and the ckETH is never minted back. This is a ledger conservation violation — burned supply is not matched by a corresponding on-chain ETH transfer. Additionally, stuck requests keep `has_pending_requests()` returning `true`, causing the timer to reschedule itself indefinitely, consuming cycles.

## Likelihood Explanation
The scenario is reachable by any unprivileged user without special privileges:
1. Submit a withdrawal at or near `cketh_minimum_withdrawal_amount` (currently 0.005 ETH after the May 2026 reduction from 0.03 ETH).
2. A gas spike (common during Ethereum network congestion) causes `max_transaction_fee` to exceed the withdrawal amount.
3. The minter loops indefinitely. The recent 6× reduction in the minimum makes this more likely, as the safety margin between the minimum and typical gas fees is narrower.

## Recommendation
In `create_transactions_batch` (`rs/ethereum/cketh/minter/src/withdraw.rs`), when `CreateTransactionError::InsufficientTransactionFee` is returned for a `WithdrawalRequest::CkEth`, the minter should:
1. Queue a `ReimbursementRequest` for the burned ckETH amount (minus ledger transfer fee) using the existing `ReimbursementIndex::CkEth` path, analogous to how failed ckERC20 withdrawals are reimbursed.
2. Emit a finalization event to remove the request from `pending_withdrawal_requests` permanently.
3. Alternatively, implement a maximum retry count or wall-clock timeout after which the request is cancelled and reimbursed.

The ckBTC pattern (`FinalizedStatus::AmountTooLow`) provides a direct template, though for ckETH a reimbursement mint is also needed since the ckETH was already burned.

## Proof of Concept
1. Call `withdraw_eth` with `amount = 5_000_000_000_000_000` wei (just above `cketh_minimum_withdrawal_amount`). ckETH is burned; `AcceptedEthWithdrawalRequest` is emitted.
2. Simulate a gas spike so that `gas_fee_estimate.to_price(21_000).max_transaction_fee() > 5_000_000_000_000_000` wei.
3. Observe that `create_transactions_batch` calls `reschedule_withdrawal_request` on every timer tick.
4. Observe that `reimbursement_requests_iter()` never yields an entry for this burn index.
5. Observe that `retrieve_eth_status(burn_index)` returns `Pending` indefinitely.
6. A deterministic integration test using PocketIC can inject a controlled `GasFeeEstimate` with a high `base_fee_per_gas` to reproduce this without mainnet interaction.