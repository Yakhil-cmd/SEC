Audit Report

## Title
ckETH Withdrawal Failure Reimburses `transaction_amount` Instead of `withdrawal_amount - effective_fee`, Causing Systematic User Underpayment — (`rs/ethereum/cketh/minter/src/state/transactions/mod.rs`)

## Summary
When a ckETH withdrawal transaction fails on Ethereum, `record_finalized_transaction` reimburses the user `finalized_tx.transaction_amount()` (= `withdrawal_amount - max_fee_estimate`) instead of `withdrawal_amount - effective_fee`. Because EIP-1559 `effective_gas_price` is almost always below `max_fee_per_gas`, the difference `max_fee_estimate - effective_fee` is silently retained by the minter, causing a systematic financial loss to every user whose ckETH withdrawal fails.

## Finding Description
**Transaction creation** (`mod.rs` lines 1122–1145): For `CkEth`, the EIP-1559 transaction `amount` field is set to `withdrawal_amount - max_transaction_fee` where `max_transaction_fee = max_fee_per_gas * gas_limit`.

**`transaction_amount()` accessor** (`tx.rs` lines 313–315): Returns `self.transaction.transaction().amount`, i.e., `withdrawal_amount - max_fee_estimate`.

**Reimbursement on failure** (`mod.rs` line 727):
```rust
reimbursed_amount: finalized_tx.transaction_amount().change_units(),
// = withdrawal_amount - max_fee_estimate  ← WRONG
```
The correct value is `withdrawal_amount - effective_fee` where `effective_fee = receipt.effective_gas_price * receipt.gas_used`, available via `finalized_tx.effective_transaction_fee()` (`tx.rs` lines 333–335).

**Why the existing test does not catch this**: The test helper `transaction_receipt` (`tests.rs` line 2906) always sets `effective_gas_price = signed_tx.transaction().max_fee_per_gas`, making `effective_fee == max_fee_estimate`. The test assertion at lines 1731–1735 uses the correct formula (`withdrawal_amount - effective_fee`), but since the two formulas are numerically identical under the test's degenerate parameters, the test passes despite the production code being wrong.

## Impact Explanation
This is a **High** severity issue. ckETH is an explicitly in-scope chain-key token. Every failed ckETH withdrawal where `effective_gas_price < max_fee_per_gas` (the normal EIP-1559 case) results in concrete, permanent financial loss to the user. Using the documentation's concrete example: `max_fee_estimate = 1,823,126,598,888,000 wei`, `effective_fee = 899,399,014,248,000 wei`, user loss = `923,727,584,640,000 wei` (~0.00092 ETH, ~$2–3 per failed withdrawal at typical ETH prices). The lost funds accumulate as unaccounted ETH in the minter, representing a protocol accounting integrity violation in an in-scope financial integration.

## Likelihood Explanation
No special attacker capability is required. Any unprivileged user who initiates a ckETH withdrawal that subsequently fails on Ethereum (out-of-gas, contract revert, etc.) is automatically affected. `effective_gas_price < max_fee_per_gas` is the normal case under EIP-1559 — the minter deliberately sets `max_fee_per_gas = 2 * base_fee + priority_fee` as a conservative upper bound. The loss is automatic and repeatable for every such failure.

## Recommendation
In `record_finalized_transaction` (`mod.rs` line 727), replace `finalized_tx.transaction_amount()` with `withdrawal_amount - effective_fee`:

```rust
// Current (wrong):
reimbursed_amount: finalized_tx.transaction_amount().change_units(),

// Correct:
reimbursed_amount: request.withdrawal_amount
    .checked_sub(finalized_tx.effective_transaction_fee())
    .unwrap_or(Wei::ZERO)
    .change_units(),
```

Also update the `transaction_receipt` test helper to use `effective_gas_price < max_fee_per_gas` to cover the non-degenerate case and ensure the corrected logic is exercised.

## Proof of Concept
Invariant violated:
```
reimbursed_amount + effective_fee == withdrawal_amount
```
Actual behavior:
```
(withdrawal_amount - max_fee_estimate) + effective_fee
= withdrawal_amount - (max_fee_estimate - effective_fee)
< withdrawal_amount   when effective_gas_price < max_fee_per_gas
```
Concrete numbers (from `cketh.adoc` failure scenario):
- `withdrawal_amount` = 39,998,000,000,000,000 wei
- `max_fee_estimate` = 1,823,126,598,888,000 wei
- `effective_fee` = 899,399,014,248,000 wei
- **Actual reimbursement**: 38,174,873,401,112,000 wei
- **Correct reimbursement**: 39,098,600,985,752,000 wei
- **User loss**: 923,727,584,640,000 wei (~0.00092 ETH)

A deterministic unit test reproducing this can be written by modifying the existing `should_record_finalized_transaction_and_reimburse_unused_tx_fee_when_cketh_withdrawal_fails` test to set `effective_gas_price` to half of `max_fee_per_gas` in the `transaction_receipt` helper, which will cause the current assertion (using the correct formula) to fail against the production code.