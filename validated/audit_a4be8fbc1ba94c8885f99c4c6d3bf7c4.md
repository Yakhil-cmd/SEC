Audit Report

## Title
Permanent Finalization DOS via `invalid` BuyerState in `sweep_icp` — (`rs/sns/swap/src/swap.rs`, `rs/sns/swap/src/types.rs`)

## Summary
The SNS Swap canister's `finalize_swap` / `sweep_icp` pipeline is permanently halted whenever any buyer's `BuyerState` produces an `invalid` result. `is_successful_sweep` treats `invalid > 0` as a fatal, non-retryable condition, causing `set_sweep_icp_result` to set an error message that causes `finalize_inner` to return early on every subsequent call. Because the `AmountTooSmall` → `invalid` path is explicitly documented as requiring a canister upgrade to recover, a single qualifying buyer entry permanently blocks SNS governance from reaching normal mode, prevents ICP and SNS token distribution, and locks funds in the Swap canister.

## Finding Description
**Root cause — `transfer_helper` returns `AmountTooSmall` when `amount_e8s <= fee`:**
`rs/sns/swap/src/types.rs` L612–615 checks `if amount <= fee { return TransferResult::AmountTooSmall