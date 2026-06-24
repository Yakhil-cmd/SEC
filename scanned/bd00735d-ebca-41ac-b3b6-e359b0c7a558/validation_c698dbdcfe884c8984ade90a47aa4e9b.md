I need to trace the exact code path for the resubmission logic, focusing on the guard condition and whether `amount = 0` is possible.

### Title
Zero-Amount ETH Transaction Sent on Resubmission When Gas Fee Equals Withdrawal Amount — (`rs/ethereum/cketh/minter/src/tx.rs`)

### Summary

The `resubmit` function uses a strict `>` guard instead of `>=` when checking whether the new transaction fee exceeds the allowed maximum. When `new_tx_price.max_transaction_fee() == withdrawal_amount` exactly, the guard does not fire, and `new_amount` is computed as `withdrawal_amount - withdrawal_amount = 0`. A zero-value ETH transaction is then signed and broadcast. The user's ckETH was already burned at withdrawal time, so they receive nothing.

The "infinite resubmission loop" framing in the question is **incorrect** — the resubmission terminates once fees exceed `withdrawal_amount`. The real bug is the off-by-one in the comparison operator that permits a zero-amount transaction to be emitted.

### Finding Description

**Entry point**: `withdraw_eth` in `rs/ethereum/cketh/minter/src/main.rs` — unprivileged, public update call.

**Guard in `resubmit`** (`rs/ethereum/cketh/minter/src/tx.rs` lines 169–179):

```rust
if new_tx_price.max_transaction_fee() > self.resubmission.allowed_max_transaction_fee() {
    return Err(ResubmitTransactionError::InsufficientTransactionFee { ... });
}
let new_amount = match self.resubmission {
    ResubmissionStrategy::ReduceEthAmount { withdrawal_amount } => {
        withdrawal_amount.checked_sub(new_tx_price.max_transaction_fee())
            .expect("BUG: ...")
    }
    ...
};
```

For `ReduceEthAmount`, `allowed_max_transaction_fee()` returns `withdrawal_amount` verbatim. [1](#0-0) 

The guard fires only when `new_fee > withdrawal_amount`. When `new_fee == withdrawal_amount`, `checked_sub` returns `Some(0)`, and the function returns `Ok(Some(tx_with_amount_zero))`. [2](#0-1) 

The same off-by-one exists in the initial `create_transaction` path: `checked_sub` returns `Some(0)` when equal, and the assertion in `record_created_transaction` only checks `withdrawal_amount > transaction.amount` (satisfied when `transaction.amount == 0`). [3](#0-2) [4](#0-3) 

`resubmit_transactions_batch` records the zero-amount transaction as a `ReplacedTransaction` event with no further validation, and it proceeds through signing and sending. [5](#0-4) 

### Impact Explanation

- The user's ckETH is burned at `withdraw_eth` time.
- If gas prices spike such that `max_fee_per_gas * 21_000 == withdrawal_amount`, the resubmitted transaction carries `amount = 0`.
- The Ethereum transaction succeeds (zero-value transfers are valid), so no reimbursement is triggered.
- The recipient receives 0 ETH; the user permanently loses their ckETH.
- The minter's ETH pool is not drained beyond the gas fee (which is covered by the burned ckETH), so no systemic pool loss occurs.

### Likelihood Explanation

The current minimum withdrawal amount is `5_000_000_000_000_000` wei (0.005 ETH, recently reduced from 0.03 ETH). [6](#0-5) 

For a minimum withdrawal, the zero-amount condition requires:

```
max_fee_per_gas = 5_000_000_000_000_000 / 21_000 ≈ 238 Gwei
```

This is extreme by current mainnet standards (~1–10 Gwei) but has been reached historically during severe congestion events. The probability is low but non-zero, and the condition is exact equality — making it a narrow but real edge case.

### Recommendation

Change the strict `>` to `>=` in the guard inside `resubmit`:

```rust
// Before (buggy):
if new_tx_price.max_transaction_fee() > self.resubmission.allowed_max_transaction_fee() {

// After (correct):
if new_tx_price.max_transaction_fee() >= self.resubmission.allowed_max_transaction_fee() {
```

Apply the same fix to `create_transaction`: after computing `tx_amount`, assert `tx_amount > Wei::ZERO` before proceeding, or treat `tx_amount == 0` as an `InsufficientTransactionFee` error.

### Proof of Concept

```
withdrawal_amount = 5_000_000_000_000_000 wei  (minimum)
gas_limit         = 21_000
max_fee_per_gas   = 238_095_238_095 wei  (~238 Gwei)

max_transaction_fee = 238_095_238_095 * 21_000 = 4_999_999_999_995_000 wei
                    ≈ withdrawal_amount  (within rounding)

// Exact equality scenario:
withdrawal_amount   = 4_200_000_000_000_000 wei
max_fee_per_gas     = 200_000_000_000 wei  (200 Gwei)
max_transaction_fee = 200_000_000_000 * 21_000 = 4_200_000_000_000_000 wei

Guard: 4_200_000_000_000_000 > 4_200_000_000_000_000 → FALSE (no error)
new_amount = 4_200_000_000_000_000 - 4_200_000_000_000_000 = 0

→ Zero-amount transaction signed and broadcast.
→ User's ckETH already burned; recipient receives 0 ETH.
```

### Citations

**File:** rs/ethereum/cketh/minter/src/tx.rs (L136-144)
```rust
impl ResubmissionStrategy {
    pub fn allowed_max_transaction_fee(&self) -> Wei {
        match self {
            ResubmissionStrategy::ReduceEthAmount { withdrawal_amount } => *withdrawal_amount,
            ResubmissionStrategy::GuaranteeEthAmount {
                allowed_max_transaction_fee,
            } => *allowed_max_transaction_fee,
        }
    }
```

**File:** rs/ethereum/cketh/minter/src/tx.rs (L169-179)
```rust
        if new_tx_price.max_transaction_fee() > self.resubmission.allowed_max_transaction_fee() {
            return Err(ResubmitTransactionError::InsufficientTransactionFee {
                allowed_max_transaction_fee: self.resubmission.allowed_max_transaction_fee(),
                actual_max_transaction_fee: new_tx_price.max_transaction_fee(),
            });
        }
        let new_amount = match self.resubmission {
            ResubmissionStrategy::ReduceEthAmount { withdrawal_amount } => {
                withdrawal_amount.checked_sub(new_tx_price.max_transaction_fee())
                    .expect("BUG: withdrawal_amount covers new transaction fee because it was checked before")
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

**File:** rs/ethereum/cketh/mainnet/minter_upgrade_2026_05_29.md (L21-24)
```markdown
* Reduce the minimum ETH withdrawal amount by a factor of 6, from 0.03 ETH (`30_000_000_000_000_000` wei) to 0.005 ETH (`5_000_000_000_000_000` wei) — approximately $10 at current prices. The reasoning is as follows:
    * The current minimum dates back to December 2023, when the ckETH minter was installed (see proposal [126171](https://dashboard.internetcomputer.org/proposal/126171)). At that time ETH traded in a similar USD range (around $2000), but Ethereum mainnet transaction fees were averaging $5–$10 per transaction ([source](https://bitinfocharts.com/comparison/ethereum-transactionfees.html#3y)).
    * Today, Ethereum mainnet fees are in the order of cents and rarely exceed $1.
    * As explained [here](https://github.com/dfinity/ic/blob/14382b5abb14b8e7de2bd4a3fb402ba069b82861/rs/ethereum/cketh/docs/cketh.adoc?plain=1#L208), an order-of-magnitude safety margin is preserved so the minter can always submit the transaction even when the Ethereum network is congested and one or more resubmissions are needed (each resubmission requires at least a 10% fee bump). With current Ethereum fees of ~$0.10–$1, a $10 minimum still preserves the ~10× safety margin even after several fee bumps.
```
