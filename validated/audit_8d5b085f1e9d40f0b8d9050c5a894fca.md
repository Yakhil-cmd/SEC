Audit Report

## Title
ckBTC Minter Burns User ckBTC Without Reimbursement on `AmountTooLow`/`DustOutput` Batch Failure - (File: `rs/bitcoin/ckbtc/minter/src/lib.rs`)

## Summary

The ckBTC minter burns a user's ckBTC at request-submission time in `retrieve_btc_with_approval`, then asynchronously processes the request in `submit_pending_requests`. When `build_unsigned_transaction` returns `BuildTxError::AmountTooLow` or `BuildTxError::DustOutput` — both reachable after a Bitcoin fee spike — the minter finalizes the request with `FinalizedStatus::AmountTooLow` and issues no reimbursement. The burned ckBTC is permanently lost. The `InvalidTransaction` arm in the same match block correctly calls `reimburse_canceled_requests`, making this an explicit asymmetric accounting gap.

## Finding Description

**Burn before enqueue.** In `retrieve_btc.rs` L314–333, `burn_ckbtcs_icrc2` executes and the request is stored as `Pending` before any batch processing occurs. The `reimbursement_account` field is populated at this point.

**Asymmetric match arms in `submit_pending_requests` (`lib.rs` L400–468):**

- `BuildTxError::InvalidTransaction` (L400–411): calls `reimburse_canceled_requests(s, batch, reason, reimbursement_fee, runtime)` — users recover their ckBTC minus a small fee.
- `BuildTxError::AmountTooLow` (L412–434): iterates the batch and calls `state::audit::remove_retrieve_btc_request(s, request, FinalizedStatus::AmountTooLow, runtime)` for each — **no reimbursement call**.
- `BuildTxError::DustOutput` (L436–468): calls `remove_retrieve_btc_request` with `FinalizedStatus::AmountTooLow` for the offending request — **no reimbursement call**.

**`FinalizedStatus::AmountTooLow` has no reimbursement path.** The enum (`state.rs` L259–267) has only `AmountTooLow` and `Confirmed { txid }` variants; neither triggers a mint-back.

**How the error becomes reachable post-submission.** The minimum withdrawal amount is computed from the current median fee rate with a `PER_REQUEST_RBF_BOUND = 22_100` sat buffer (`fees/mod.rs` L133–143). A request submitted at low fees passes the minimum check. If fees spike before `submit_pending_requests` fires, `build_unsigned_transaction` at `lib.rs` L1306–1307 returns `AmountTooLow` when `fee + minter_fee > amount`, or `DustOutput` at L1322–1326 when a single output falls below the dust limit after fee deduction.

**Reimbursement infrastructure is fully wired for `InvalidTransaction`.** `reimburse_canceled_requests` (`lib.rs` L292–329) already handles the mint-back pattern, iterates requests, reads `reimbursement_account`, and calls `state::audit::reimburse_withdrawal`. The `reimbursement_account` is populated for all `retrieve_btc_with_approval` requests (`retrieve_btc.rs` L327–330). The only missing step is calling this function from the `AmountTooLow` and `DustOutput` arms.

## Impact Explanation

This is a direct, unrecoverable token loss for affected users: ckBTC is burned on the ledger (supply reduced), no BTC is ever sent, and no ckBTC is minted back. The terminal status `RetrieveBtcStatusV2::AmountTooLow` provides no recourse. This matches the allowed High impact: **"Significant Chain Fusion, ck-token, ledger … security impact with concrete user or protocol harm."** During a fee spike event, multiple users' requests can be simultaneously affected, aggregating the loss.

## Likelihood Explanation

Bitcoin fee spikes of 5–20× within a single processing window are historically documented (Ordinals waves, halving periods). The `fee_based_retrieve_btc_min_amount` is updated only when `estimate_fee_per_vbyte` is called during `submit_pending_requests`; a request submitted just before a spike and processed just after is fully exposed. The `DustOutput` path is reachable even with moderate fee increases for requests near the minimum. The entry point `retrieve_btc_with_approval` is open to any unprivileged caller with no special privileges required.

## Recommendation

In `lib.rs`, replace the bare `remove_retrieve_btc_request` loops in the `AmountTooLow` and `DustOutput` arms with calls to `reimburse_canceled_requests`, mirroring the `InvalidTransaction` arm. A `WithdrawalReimbursementReason` variant (e.g., `FeeSpike` or reuse of `InvalidTransaction`) should be added or reused. A small reimbursement fee (computed via `fee_estimator.reimbursement_fee_for_pending_withdrawal_requests`) should be charged to cover minter work already performed. The `reimbursement_account` field is already populated for all `retrieve_btc_with_approval` requests, so no structural changes to `RetrieveBtcRequest` are needed.

## Proof of Concept

1. Bitcoin mainnet fees are low (5 sat/vB). `fee_based_retrieve_btc_min_amount` = 50,000 sats.
2. Unprivileged user calls `retrieve_btc_with_approval(amount=50_000, address=<valid P2WPKH>)`. `burn_ckbtcs_icrc2` executes; ckBTC burn is recorded on the ledger. Request stored as `Pending`.
3. Before `submit_pending_requests` fires, Bitcoin network fees spike to 400 sat/vB.
4. `build_unsigned_transaction` computes `fee ≈ 400 × 141 vbytes = 56,400 sats`; `56,400 > 50,000` → returns `BuildTxError::AmountTooLow` (`lib.rs` L1306–1307).
5. `submit_pending_requests` enters the `AmountTooLow` arm (`lib.rs` L412–434), calls `remove_retrieve_btc_request(..., FinalizedStatus::AmountTooLow)` — no `reimburse_canceled_requests` call.
6. User queries `retrieve_btc_status_v2(block_index)` → returns `RetrieveBtcStatusV2::AmountTooLow`. No `WillReimburse` or `Reimbursed` state ever follows. 50,000 sats of ckBTC are permanently burned with no BTC sent and no ckBTC returned.

A deterministic integration test can reproduce this by: (a) submitting a withdrawal at low fee rate, (b) advancing the mock fee estimator to a high rate before triggering `submit_pending_requests`, and (c) asserting that `retrieve_btc_status_v2` never transitions to `Reimbursed` while the ledger burn is confirmed.