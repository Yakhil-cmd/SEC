Audit Report

## Title
ckETH Withdrawal Requests Permanently Stuck in Pending Queue with No Reimbursement Path When Gas Fees Exceed Withdrawal Amount - (`rs/ethereum/cketh/minter/src/withdraw.rs`)

## Summary

When `withdraw_eth` is called, the user's ckETH is immediately and irreversibly burned from the ledger. If the current Ethereum gas fee estimate exceeds the withdrawal amount, `create_transactions_batch` calls `reschedule_withdrawal_request`, which moves the request to the back of the pending queue indefinitely. There is no timeout, no automatic reimbursement trigger, and no user-callable cancellation endpoint — the user's funds are locked until a governance upgrade intervenes.

## Finding Description

**Burn is immediate and irreversible:** In `rs/ethereum/cketh/minter/src/main.rs` (L301–336), `withdraw_eth` burns the user's ckETH from the ledger before enqueuing the `EthWithdrawalRequest`. The burn is not conditional on the transaction ever being submitted to Ethereum.

**Insufficient-fee error path reschedules indefinitely:** In `rs/ethereum/cketh/minter/src/withdraw.rs` (L281–291), when `create_transaction` returns `CreateTransactionError::InsufficientTransactionFee`, the handler calls `mutate_state(|s| s.eth_transactions.reschedule_withdrawal_request(request))`. No reimbursement is triggered.

**`reschedule_withdrawal_request` has no state change:** In `rs/ethereum/cketh/minter/src/state/transactions/mod.rs` (L469–483), the function simply removes and re-appends the request to `pending_withdrawal_requests` with no expiry tracking, no counter, and no transition to any failure state.

**Reimbursement path is gated on Ethereum finalization:** In `rs/ethereum/cketh/minter/src/state/transactions/mod.rs` (L718–731), `record_reimbursement_request` is only called when a transaction has been finalized on Ethereum with `TransactionStatus::Failure`. A request that never leaves `pending_withdrawal_requests` never reaches `maybe_reimburse` and therefore never reaches `reimbursement_requests`. The `process_reimbursement` function in `rs/ethereum/cketh/minter/src/withdraw.rs` (L46–65) only processes entries already in `reimbursement_requests`.

**No cancellation endpoint exists:** A grep for `cancel`, `timeout`, `expir`, `max_time`, and `pending_duration` across all files under `rs/ethereum/cketh/minter/src/` returns zero matches. The DID file confirms the only withdrawal-related endpoints are `withdraw_eth`, `withdraw_erc20`, `retrieve_eth_status`, and `withdrawal_status`.

**Existing guards are insufficient:** The `EthTransactions` state machine (`rs/ethereum/cketh/minter/src/state/transactions/mod.rs`, L361–377) has no concept of a timed-out or failed-pending state. The `pending_withdrawal_requests` `VecDeque` has no associated deadline or retry counter.

## Impact Explanation

This is a **permanent lock of user funds** reachable through normal, unprivileged product flows. The user's ckETH is burned from the ledger (balance goes to zero), no ETH is ever sent on Ethereum, and no reimbursement is minted back. The only recovery path is a governance-approved canister upgrade. This matches the allowed High impact class: *"Significant Chain Fusion, ck-token, ledger … security impact with concrete user or protocol harm."* The per-user loss is bounded by the withdrawal amount (minimum ~$10), but the scenario can affect an unbounded number of users simultaneously during a gas spike, with no self-service recovery.

## Likelihood Explanation

No privileged access is required. Any unprivileged IC principal calling `withdraw_eth` with an amount near the minimum threshold is a potential victim. Ethereum gas fee spikes are historically common (NFT mints, network congestion). At 300 gwei base fee, the cost of a 21,000-gas ETH transfer is `300e9 × 21,000 = 6.3 × 10¹⁵ wei`, which exceeds the current minimum of `5 × 10¹⁵ wei`. The minimum was recently reduced by 6× (from 0.03 ETH to 0.005 ETH), shrinking the safety margin and increasing the probability of this condition being triggered. The condition is self-sustaining: once triggered, it repeats on every timer tick (~5 minutes) with no escape.

## Recommendation

1. **Add a maximum pending duration:** Track `created_at` (already present on `EthWithdrawalRequest`) and after a configurable timeout (e.g., 7 days), automatically transition the request from `pending_withdrawal_requests` to `reimbursement_requests`, minting back the full withdrawal amount to the user.
2. **Add a user-callable cancellation endpoint:** Allow the original requester to cancel a `Pending` withdrawal and trigger reimbursement, analogous to how ckBTC handles `TooManyInputs` cancellations (see `rs/bitcoin/ckbtc/minter/tests/tests.rs`, L3108–3173).
3. **Enforce a stricter dynamic minimum:** Ensure `cketh_minimum_withdrawal_amount` always exceeds the maximum plausible gas cost (e.g., at the 99th-percentile historical `base_fee_per_gas`) by the required safety margin, and re-evaluate the minimum dynamically.

## Proof of Concept

1. User calls `withdraw_eth` with `amount = 5_000_000_000_000_000` wei (current minimum).
2. Minter burns `5_000_000_000_000_000` wei from the ckETH ledger. User balance = 0.
3. `EthWithdrawalRequest` is enqueued in `pending_withdrawal_requests`.
4. Ethereum `base_fee_per_gas` spikes to 300 gwei. Gas cost = `300e9 × 21,000 = 6.3 × 10¹⁵ wei > 5 × 10¹⁵ wei`.
5. `create_transactions_batch` → `create_transaction` → `CreateTransactionError::InsufficientTransactionFee`.
6. Handler calls `reschedule_withdrawal_request` → request moved to back of queue.
7. Steps 5–6 repeat every ~5 minutes indefinitely.
8. User queries `retrieve_eth_status` → `Pending` forever.
9. User has no endpoint to cancel or recover funds.
10. Funds remain locked until a governance upgrade manually intervenes.

A deterministic integration test can reproduce this by: (a) setting up a minter state with a near-minimum `EthWithdrawalRequest` in `pending_withdrawal_requests`, (b) injecting a `GasFeeEstimate` where `max_transaction_fee > withdrawal_amount`, (c) calling `create_transactions_batch`, and (d) asserting the request remains in `pending_withdrawal_requests` with no entry added to `reimbursement_requests` after an arbitrarily large number of timer ticks.