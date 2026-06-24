Audit Report

## Title
ckETH Failed-Withdrawal Reimbursement Uses `transaction_amount` Instead of `withdrawal_amount - actual_fee`, Causing User ckETH Loss — (`rs/ethereum/cketh/minter/src/state/transactions/mod.rs`)

## Summary
When a ckETH withdrawal transaction fails on-chain, `record_finalized_transaction` reimburses the user `finalized_tx.transaction_amount()`, which equals `withdrawal_amount - max_fee_per_gas * gas_limit` (or `withdrawal_amount - max_fee_N * gas_limit` after N resubmissions). The correct reimbursement is `withdrawal_amount - effective_gas_price * gas_used` (the actual on-chain cost). The gap — the unspent portion of the maximum fee — is permanently retained in the minter's ETH balance and never returned to the user.

## Finding Description
In `EthTransactions::record_finalized_transaction` at line 727 of `rs/ethereum/cketh/minter/src/state/transactions/mod.rs`:

```rust
reimbursed_amount: finalized_tx.transaction_amount().change_units(),
```

`finalized_tx.transaction_amount()` (defined at `rs/ethereum/cketh/minter/src/tx.rs` lines 313–315) returns `self.transaction.transaction().amount` — the EIP-1559 `amount` field of the signed transaction. For ckETH withdrawals, this field is set at transaction creation (`mod.rs` lines 1125–1142) to `withdrawal_amount - max_fee_per_gas * gas_limit`. On each resubmission via `ReduceEthAmount` (`tx.rs` lines 175–181), it is recomputed as `withdrawal_amount - new_max_fee * gas_limit`, shrinking the `amount` field with each fee bump.

The actual on-chain cost of a failed transaction is `effective_gas_price * gas_used` (from the receipt), available via `finalized_tx.effective_transaction_fee()`. EIP-1559 guarantees `effective_gas_price ≤ max_fee_per_gas`, and failed transactions often consume less than `gas_limit`. The difference `max_fee_N * gas_limit - effective_gas_price * gas_used` is always ≥ 0 and is never refunded.

The unit test `should_record_finalized_transaction_and_reimburse_unused_tx_fee_when_cketh_withdrawal_fails` (tests.rs lines 1689–1738) asserts the correct formula (`withdrawal_amount - effective_fee_paid`) but only passes because the `transaction_receipt` helper (tests.rs lines 2895–2911) sets `effective_gas_price = max_fee_per_gas` and `gas_used = gas_limit`, making `effective_fee_paid == max_transaction_fee == withdrawal_amount - transaction_amount()`. The two quantities are accidentally equal in this degenerate case; no test covers the resubmission-then-failure path.

## Impact Explanation
Any user whose ckETH withdrawal transaction fails on-chain (after zero or more resubmissions) permanently loses `max_fee_N * gas_limit - effective_gas_price * gas_used` ckETH. The minter's ETH balance gains the corresponding ETH, creating a supply/backing imbalance. For a typical ETH transfer (gas_limit = 21,000) with one resubmission at a 10% fee bump and an actual execution price at the original estimate, the shortfall is `0.1 * max_fee_0 * 21,000` wei — at high gas prices this can be hundreds of USD per affected withdrawal. This is a concrete, permanent user-funds loss in the ckETH financial integration, matching the **High** impact class: "Significant Chain Fusion, ck-token, ledger … security impact with concrete user or protocol harm."

## Likelihood Explanation
No special privileges are required. Any user calling `withdraw_eth` is exposed if their transaction fails on-chain. Gas spikes causing resubmissions are routine on Ethereum mainnet; EVM transaction failures (out-of-gas, reverts) are also common. The combination is realistic and repeatable without any attacker involvement — the loss is a direct consequence of the minter's own accounting logic.

## Recommendation
Replace line 727 with the correct formula using the actual on-chain fee from the receipt:

```rust
reimbursed_amount: request.withdrawal_amount
    .checked_sub(finalized_tx.effective_transaction_fee())
    .unwrap_or(Wei::ZERO)
    .change_units(),
```

`finalized_tx.effective_transaction_fee()` (tx.rs line 333–335) returns `receipt.effective_gas_price * receipt.gas_used`, the true on-chain cost. Additionally, update the `transaction_receipt` test helper to use `effective_gas_price < max_fee_per_gas` and `gas_used < gas_limit` so the test actually exercises the unused-fee refund path, and add a dedicated test for the resubmission-then-failure scenario.

## Proof of Concept
State-machine test (no privileged access needed):

1. Create a ckETH withdrawal request for `W = 100_000_000_000_000_000` wei with `max_fee_per_gas = 100 gwei`, `gas_limit = 21_000`. Initial `tx.amount = W - 100e9 * 21_000`.
2. Resubmit twice with 10% fee bumps: `max_fee_2 ≈ 121 gwei`. After resubmission, `tx.amount = W - 121e9 * 21_000`.
3. Finalize with a `Failure` receipt where `effective_gas_price = 100 gwei` (realistic — the actual base fee is the original estimate, not the bumped ceiling) and `gas_used = 21_000`.
4. `effective_fee_paid = 100e9 * 21_000 = 2_100_000_000_000_000` wei.
5. **Expected** `reimbursed_amount = W - 2_100_000_000_000_000`.
6. **Actual** `reimbursed_amount = tx.amount = W - 121e9 * 21_000 = W - 2_541_000_000_000_000`.
7. User loses `(121e9 - 100e9) * 21_000 = 441_000_000_000_000` wei (~$1.10 at $2,500/ETH and 100 gwei gas) per failed withdrawal — permanently.