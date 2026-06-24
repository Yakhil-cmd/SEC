Audit Report

## Title
ckETH Permanently Burned Without Reimbursement When `erc20_tx_fee ≤ CKETH_LEDGER_TRANSACTION_FEE` and ckERC20 Burn Fails - (File: rs/ethereum/cketh/minter/src/main.rs)

## Summary
In `withdraw_erc20`, when the ckETH gas-fee burn succeeds but the subsequent ckERC20 burn fails with a user-attributable error (`InsufficientFunds`, `AmountTooLow`, or `InsufficientAllowance`), the reimbursement amount is computed as `erc20_tx_fee.checked_sub(CKETH_LEDGER_TRANSACTION_FEE).unwrap_or(Wei::ZERO)`. When `erc20_tx_fee ≤ CKETH_LEDGER_TRANSACTION_FEE`, this yields `Wei::ZERO`, the guard `if reimbursed_amount > Wei::ZERO` is false, no `FailedErc20WithdrawalRequest` event is emitted, and the already-burned ckETH is permanently destroyed with no reimbursement ever created.

## Finding Description
`CKETH_LEDGER_TRANSACTION_FEE` is defined as `Wei::new(2_000_000_000_000_u128)` (2 × 10¹² wei) at line 59 of `rs/ethereum/cketh/minter/src/main.rs`. The `withdraw_erc20` function (lines 390–542) performs two sequential burns:

1. **Lines 448–458**: Burns `erc20_tx_fee` ckETH from the user's account via `cketh_ledger.burn_from(...)`.
2. **Lines 468–477**: Burns the ckERC20 amount from the user's account via `ckerc20_ledger.burn_from(...)`.

On step-2 failure with a user-attributable error, lines 507–513 compute:
```rust
LedgerBurnError::InsufficientFunds { .. }
| LedgerBurnError::AmountTooLow { .. }
| LedgerBurnError::InsufficientAllowance { .. } => erc20_tx_fee
    .checked_sub(CKETH_LEDGER_TRANSACTION_FEE)
    .unwrap_or(Wei::ZERO),
```
When `erc20_tx_fee ≤ CKETH_LEDGER_TRANSACTION_FEE`, `checked_sub` returns `None`, mapped to `Wei::ZERO`. The guard at line 515 (`if reimbursed_amount > Wei::ZERO`) is then false, so the `FailedErc20WithdrawalRequest` event is never emitted and no `ReimbursementRequest` is ever inserted into state. There is no pre-flight check anywhere in `withdraw_erc20` (lines 398–432) that enforces `erc20_tx_fee > CKETH_LEDGER_TRANSACTION_FEE` before executing the first burn. The `process_reimbursement` function in `rs/ethereum/cketh/minter/src/withdraw.rs` (lines 46–63) only iterates over existing `reimbursement_requests`; since none is inserted, the loss is irrecoverable without a canister upgrade.

## Impact Explanation
This is a permanent, unconditional loss of user ckETH funds on the ICRC-1 ledger with no corresponding ETH released on Ethereum and no reimbursement minted back. Each incident destroys at most `CKETH_LEDGER_TRANSACTION_FEE` = 2 µETH of user funds. The ckETH total supply decreases without any backing ETH being released, violating the 1:1 peg invariant of the chain-fusion bridge. This constitutes a concrete, demonstrable Chain Fusion / ck-token ledger conservation bug with direct user fund loss, matching the **High** impact category: "Significant Chain Fusion, ck-token, ledger... security impact with concrete user or protocol harm."

## Likelihood Explanation
The trigger condition requires `erc20_tx_fee ≤ 2 × 10¹² wei`. Since `erc20_tx_fee = gas_price × 65,000`, this requires `gas_price ≤ ~30.77 gwei` — a condition that has occurred repeatedly on Ethereum mainnet during low-activity periods (including multiple times in 2023–2024 when base fees dropped to single-digit gwei). No special privileges are required: any unprivileged user who calls `withdraw_erc20` during such a low-gas window with an insufficient ckERC20 allowance or balance (a common user mistake) triggers the bug. The condition is externally observable (Ethereum gas trackers), making it targetable.

## Recommendation
Add a pre-flight guard before the first ckETH burn to ensure a reimbursement is always possible:
```rust
if erc20_tx_fee <= CKETH_LEDGER_TRANSACTION_FEE {
    return Err(WithdrawErc20Error::TemporarilyUnavailable(
        "Estimated gas fee too low to cover ledger transaction fee".to_string()
    ));
}
```
Alternatively, when `reimbursed_amount` is zero due to the subtraction, still emit a `FailedErc20WithdrawalRequest` for the full `erc20_tx_fee` (absorbing the ledger fee as a protocol cost), consistent with how the `TemporarilyUnavailable` branch already handles this at line 508.

## Proof of Concept
1. Wait for (or observe) Ethereum gas prices to drop such that `estimate_erc20_transaction_fee()` returns `F ≤ 2_000_000_000_000 wei` (~30 gwei or below).
2. As an unprivileged user, call `withdraw_erc20` with a valid `ckerc20_ledger_id`, a valid `recipient`, and an `amount` for which the minter has no ckERC20 allowance (or the user has insufficient balance).
3. The minter executes `cketh_ledger.burn_from(cketh_account, F, ...)` — this succeeds; user loses `F` ckETH.
4. The minter executes `ckerc20_ledger.burn_from(ckerc20_account, amount, ...)` — this fails with `InsufficientAllowance` or `InsufficientFunds`.
5. `reimbursed_amount = F.checked_sub(2_000_000_000_000).unwrap_or(Wei::ZERO) = Wei::ZERO`.
6. `if reimbursed_amount > Wei::ZERO` is false; no `FailedErc20WithdrawalRequest` event is emitted.
7. Function returns `Err(WithdrawErc20Error::CkErc20LedgerError { ... })`.
8. User's `F` ckETH is permanently burned. A deterministic integration test using `PocketIC` can reproduce this by mocking `estimate_erc20_transaction_fee` to return a value ≤ `CKETH_LEDGER_TRANSACTION_FEE` and calling `withdraw_erc20` with zero ckERC20 allowance, then asserting no reimbursement request exists in state.