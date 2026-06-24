Audit Report

## Title
Off-by-One Guard Allows Zero-Amount ETH Transaction on Resubmission — (`rs/ethereum/cketh/minter/src/tx.rs`)

## Summary
The `resubmit` function in `tx.rs` uses a strict `>` comparison instead of `>=` when checking whether the new transaction fee exceeds the allowed maximum. When `new_tx_price.max_transaction_fee() == withdrawal_amount` exactly, the guard does not fire, `checked_sub` returns `Some(0)`, and a zero-value ETH transaction is constructed, signed, and broadcast. Because ckETH is burned at `withdraw_eth` time, the user permanently loses their funds while the recipient receives 0 ETH.

## Finding Description

**Root cause — `tx.rs` line 169:**

The guard condition is strict `>`:

```rust
if new_tx_price.max_transaction_fee() > self.resubmission.allowed_max_transaction_fee() {
    return Err(ResubmitTransactionError::InsufficientTransactionFee { ... });
}
``` [1](#0-0) 

For `ReduceEthAmount`, `allowed_max_transaction_fee()` returns `withdrawal_amount` verbatim:

```rust
ResubmissionStrategy::ReduceEthAmount { withdrawal_amount } => *withdrawal_amount,
``` [2](#0-1) 

When `new_fee == withdrawal_amount`, the guard evaluates `false`, execution falls through, and:

```rust
withdrawal_amount.checked_sub(new_tx_price.max_transaction_fee())
    .expect("BUG: ...")
``` [3](#0-2) 

`checked_sub` returns `Some(0)`, which is then set as `amount` in the returned `Eip1559TransactionRequest`. [4](#0-3) 

**No downstream guard catches this.** `resubmit_transactions_batch` records the zero-amount transaction directly as a `ReplacedTransaction` event with no amount validation: [5](#0-4) 

The only existing assertion on transaction amount is in `record_created_transaction` at line 509, which checks `req.withdrawal_amount > transaction.amount`. When `transaction.amount == 0` and `withdrawal_amount > 0`, this assertion passes — it does not guard against zero. Furthermore, this assertion is not present in the resubmission code path at all. [6](#0-5) 

**The same off-by-one exists in `create_transaction`** (`mod.rs` line 1125): `checked_sub` returns `Some(0)` when `max_transaction_fee == withdrawal_amount`, producing a zero-amount transaction on the initial creation path as well. [7](#0-6) 

**Exploit flow:**
1. User calls `withdraw_eth`; ckETH is burned.
2. Gas prices spike such that `max_fee_per_gas * 21_000 == withdrawal_amount` exactly.
3. On resubmission, `resubmit()` returns `Ok(Some(tx { amount: 0 }))`.
4. `resubmit_transactions_batch` records and processes the zero-amount transaction.
5. The Ethereum transaction is signed and broadcast with `value = 0`.
6. The transaction succeeds on-chain (zero-value transfers are valid EIP-1559 transactions).
7. No reimbursement is triggered because the transaction did not revert.
8. User's ckETH is permanently lost; recipient receives 0 ETH.

## Impact Explanation

This is a permanent, irreversible loss of user funds in the ckETH minter, an explicitly in-scope Chain Fusion / ck-token financial integration. The burned ckETH is not reimbursed because the Ethereum transaction succeeds. This matches the allowed impact: **"Significant Chain Fusion, ck-token, ledger... security impact with concrete user or protocol harm"** — **High ($2,000–$10,000)**.

## Likelihood Explanation

The condition requires exact equality: `max_fee_per_gas * 21_000 == withdrawal_amount`. For the current minimum withdrawal of `5_000_000_000_000_000` wei (0.005 ETH), this requires ~238 Gwei, which is extreme under normal conditions but has been reached during historical congestion events. The condition is narrow (exact equality), making it a low-probability but non-zero real-world edge case. No attacker action is required — normal gas price volatility is sufficient. The bug is also present on the initial `create_transaction` path, widening the surface slightly.

## Recommendation

**Primary fix** — change `>` to `>=` in `resubmit` (`tx.rs` line 169):

```rust
// Before (buggy):
if new_tx_price.max_transaction_fee() > self.resubmission.allowed_max_transaction_fee() {

// After (correct):
if new_tx_price.max_transaction_fee() >= self.resubmission.allowed_max_transaction_fee() {
```

**Secondary fix** — in `create_transaction` (`mod.rs` line 1125), treat `tx_amount == Wei::ZERO` as an `InsufficientTransactionFee` error, or add an explicit `assert!(tx_amount > Wei::ZERO)` before constructing the transaction.

**Defensive fix** — add a zero-amount guard in `record_created_transaction` and the resubmission recording path.

## Proof of Concept

Unit test (safe, no mainnet interaction):

```rust
// Exact equality scenario:
let withdrawal_amount = Wei::from(4_200_000_000_000_000_u128);
let gas_limit = GasAmount::from(21_000_u64);
let max_fee_per_gas = Wei::from(200_000_000_000_u128); // 200 Gwei
// max_transaction_fee = 200_000_000_000 * 21_000 = 4_200_000_000_000_000 == withdrawal_amount

let signed_tx = SignedTransactionRequest {
    transaction: /* signed tx with original amount */,
    resubmission: ResubmissionStrategy::ReduceEthAmount { withdrawal_amount },
};
let new_gas_fee = GasFeeEstimate {
    max_fee_per_gas,
    max_priority_fee_per_gas: ...,
};
let result = signed_tx.resubmit(new_gas_fee);
// Assert: result == Ok(Some(tx)) where tx.amount == Wei::ZERO
// This demonstrates the bug: no error returned, zero-amount tx produced.
```

### Citations

**File:** rs/ethereum/cketh/minter/src/tx.rs (L139-139)
```rust
            ResubmissionStrategy::ReduceEthAmount { withdrawal_amount } => *withdrawal_amount,
```

**File:** rs/ethereum/cketh/minter/src/tx.rs (L169-174)
```rust
        if new_tx_price.max_transaction_fee() > self.resubmission.allowed_max_transaction_fee() {
            return Err(ResubmitTransactionError::InsufficientTransactionFee {
                allowed_max_transaction_fee: self.resubmission.allowed_max_transaction_fee(),
                actual_max_transaction_fee: new_tx_price.max_transaction_fee(),
            });
        }
```

**File:** rs/ethereum/cketh/minter/src/tx.rs (L177-178)
```rust
                withdrawal_amount.checked_sub(new_tx_price.max_transaction_fee())
                    .expect("BUG: withdrawal_amount covers new transaction fee because it was checked before")
```

**File:** rs/ethereum/cketh/minter/src/tx.rs (L182-188)
```rust
        Ok(Some(Eip1559TransactionRequest {
            max_priority_fee_per_gas: new_tx_price.max_priority_fee_per_gas,
            max_fee_per_gas: new_tx_price.max_fee_per_gas,
            gas_limit: new_tx_price.gas_limit,
            amount: new_amount,
            ..transaction_request.clone()
        }))
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L225-246)
```rust
    for result in transactions_to_resubmit {
        match result {
            Ok((withdrawal_id, transaction)) => {
                log!(
                    INFO,
                    "[resubmit_transactions_batch]: transactions to resubmit {transaction:?}"
                );
                mutate_state(|s| {
                    process_event(
                        s,
                        EventType::ReplacedTransaction {
                            withdrawal_id,
                            transaction,
                        },
                    )
                });
            }
            Err(e) => {
                log!(INFO, "Failed to resubmit transaction: {e:?}");
            }
        }
    }
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L506-511)
```rust
        match &withdrawal_request {
            WithdrawalRequest::CkEth(req) => {
                assert!(
                    req.withdrawal_amount > transaction.amount,
                    "BUG: transaction amount should be the withdrawal amount deducted from transaction fees"
                );
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1125-1134)
```rust
            let tx_amount = match request.withdrawal_amount.checked_sub(max_transaction_fee) {
                Some(tx_amount) => tx_amount,
                None => {
                    return Err(CreateTransactionError::InsufficientTransactionFee {
                        cketh_ledger_burn_index: request.ledger_burn_index,
                        allowed_max_transaction_fee: request.withdrawal_amount,
                        actual_max_transaction_fee: max_transaction_fee,
                    });
                }
            };
```
