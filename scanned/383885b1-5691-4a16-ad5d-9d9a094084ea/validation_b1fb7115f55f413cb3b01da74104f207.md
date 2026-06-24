### Title
ckETH Withdrawal Permanently Stuck When Gas Fee Spikes Above Withdrawal Amount — (`rs/ethereum/cketh/minter/src/withdraw.rs`)

---

### Summary

When a ckETH withdrawal request is accepted and the user's ckETH is burned, but the current Ethereum gas fee estimate later exceeds the withdrawal amount, the minter silently reschedules the request to the back of the queue indefinitely. There is no reimbursement path for this case. If gas fees remain elevated, the user's ckETH is permanently burned with no ETH ever sent and no refund issued.

---

### Finding Description

The ckETH minter's `withdraw_eth` endpoint burns the user's ckETH immediately upon acceptance, then queues an `EthWithdrawalRequest`. The background task `create_transactions_batch` later attempts to build an Ethereum transaction. The fee is deducted from the withdrawal amount at transaction-creation time, not at burn time.

In `create_transaction` (`rs/ethereum/cketh/minter/src/state/transactions/mod.rs`):

```rust
let tx_amount = match request.withdrawal_amount.checked_sub(max_transaction_fee) {
    Some(tx_amount) => tx_amount,
    None => {
        return Err(CreateTransactionError::InsufficientTransactionFee { ... });
    }
};
``` [1](#0-0) 

When this error is returned, `create_transactions_batch` reschedules the request to the back of the queue with no reimbursement:

```rust
Err(CreateTransactionError::InsufficientTransactionFee { ... }) => {
    log!(..., "Request moved back to end of queue.");
    mutate_state(|s| s.eth_transactions.reschedule_withdrawal_request(request));
}
``` [2](#0-1) 

The `maybe_reimburse` set — which is the only mechanism that triggers reimbursement — is only populated inside `record_created_transaction`, which is never reached in this error path. [3](#0-2) 

The minimum withdrawal amount check at `withdraw_eth` entry:

```rust
let minimum_withdrawal_amount = read_state(|s| s.cketh_minimum_withdrawal_amount);
if amount < minimum_withdrawal_amount {
    return Err(WithdrawalError::AmountTooLow { ... });
}
``` [4](#0-3) 

…only guards against amounts below the static configured minimum. It does not guard against the dynamic gas fee estimate at transaction-creation time, which can spike well above the minimum. The developers explicitly assumed this case cannot occur, as evidenced by the `panic!` in the `AmountTooLow` ledger-burn error handler:

```rust
LedgerBurnError::AmountTooLow { ... } => {
    panic!(
        "BUG: withdrawal amount {failed_burn_amount} on the ckETH ledger {ledger:?} should always be higher than the ledger transaction fee {minimum_burn_amount}"
    )
}
``` [5](#0-4) 

This assumption does not hold for the Ethereum network gas fee, which is separate from the ledger transfer fee and can be orders of magnitude larger.

---

### Impact Explanation

A user whose withdrawal amount is between `cketh_minimum_withdrawal_amount` and the current `max_transaction_fee` (e.g., during an Ethereum gas spike) will have their ckETH permanently burned with no ETH sent and no reimbursement. The request cycles in the queue indefinitely. If gas fees never return below the withdrawal amount, the funds are lost. This is a ledger conservation violation: ckETH supply decreases without a corresponding ETH transfer.

---

### Likelihood Explanation

The scenario is reachable by any unprivileged user calling `withdraw_eth`. Ethereum gas fees are volatile and can spike 10–100× within minutes. The minimum withdrawal amount was recently reduced from 0.03 ETH to 0.005 ETH (proposal 139665), making the gap between the minimum and a gas-spike fee much smaller and this scenario more likely. No privileged access, oracle manipulation, or consensus attack is required — only a gas fee spike after the burn. [6](#0-5) 

---

### Recommendation

When `create_transaction` returns `InsufficientTransactionFee` and the request has been rescheduled more than a configurable number of times (or after a timeout), trigger the existing reimbursement mechanism instead of rescheduling. Specifically, add the withdrawal ID to `maybe_reimburse` and create a `ReimbursementRequest` deducting a small penalty fee, mirroring the pattern used for failed ERC-20 withdrawals.

Alternatively, add a staleness deadline to each `EthWithdrawalRequest` and reimburse any request that has been rescheduled past that deadline.

---

### Proof of Concept

1. Ethereum gas fees are low; user calls `withdraw_eth` with `amount = cketh_minimum_withdrawal_amount` (e.g., 5_000_000_000_000_000 wei after the recent reduction).
2. ckETH is burned; `EthWithdrawalRequest` is queued.
3. Ethereum gas spikes: `max_transaction_fee = gas_limit * max_fee_per_gas = 21_000 * 300_000_000_000 = 6_300_000_000_000_000 wei > 5_000_000_000_000_000 wei`.
4. `create_transactions_batch` calls `create_transaction`; `checked_sub` returns `None`; `InsufficientTransactionFee` is returned.
5. Request is rescheduled to back of queue. No reimbursement entry is created.
6. Steps 4–5 repeat on every timer tick. User's ckETH is gone; no ETH is ever sent; no refund is ever issued. [7](#0-6) [8](#0-7)

### Citations

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L551-552)
```rust
        );
        assert!(self.maybe_reimburse.insert(withdrawal_id));
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1104-1134)
```rust
/// Creates an EIP-1559 transaction for the given withdrawal request.
/// The transaction fees are paid by the beneficiary,
/// meaning that the fees will be deducted from the withdrawal amount.
///
/// # Errors
/// * `CreateTransactionError::InsufficientTransactionFee` if the ETH withdrawal amount does not cover the transaction fee.
pub fn create_transaction(
    withdrawal_request: &WithdrawalRequest,
    nonce: TransactionNonce,
    gas_fee_estimate: GasFeeEstimate,
    gas_limit: GasAmount,
    ethereum_network: EthereumNetwork,
) -> Result<Eip1559TransactionRequest, CreateTransactionError> {
    assert!(
        gas_limit > GasAmount::ZERO,
        "BUG: gas limit should be non-zero"
    );
    match withdrawal_request {
        WithdrawalRequest::CkEth(request) => {
            let transaction_price = gas_fee_estimate.to_price(gas_limit);
            let max_transaction_fee = transaction_price.max_transaction_fee();
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

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L249-293)
```rust
fn create_transactions_batch(gas_fee_estimate: GasFeeEstimate) {
    for request in read_state(|s| {
        s.eth_transactions
            .withdrawal_requests_batch(WITHDRAWAL_REQUESTS_BATCH_SIZE)
    }) {
        log!(DEBUG, "[create_transactions_batch]: processing {request:?}",);
        let ethereum_network = read_state(State::ethereum_network);
        let nonce = read_state(|s| s.eth_transactions.next_transaction_nonce());
        let gas_limit = estimate_gas_limit(&request);
        match create_transaction(
            &request,
            nonce,
            gas_fee_estimate.clone(),
            gas_limit,
            ethereum_network,
        ) {
            Ok(transaction) => {
                log!(
                    DEBUG,
                    "[create_transactions_batch]: created transaction {transaction:?}",
                );

                mutate_state(|s| {
                    process_event(
                        s,
                        EventType::CreatedTransaction {
                            withdrawal_id: request.cketh_ledger_burn_index(),
                            transaction,
                        },
                    );
                });
            }
            Err(CreateTransactionError::InsufficientTransactionFee {
                cketh_ledger_burn_index: ledger_burn_index,
                allowed_max_transaction_fee: withdrawal_amount,
                actual_max_transaction_fee: max_transaction_fee,
            }) => {
                log!(
                    INFO,
                    "[create_transactions_batch]: Withdrawal request with burn index {ledger_burn_index} has insufficient amount {withdrawal_amount:?} to cover transaction fees: {max_transaction_fee:?}. Request moved back to end of queue."
                );
                mutate_state(|s| s.eth_transactions.reschedule_withdrawal_request(request));
            }
        };
    }
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L291-296)
```rust
    let minimum_withdrawal_amount = read_state(|s| s.cketh_minimum_withdrawal_amount);
    if amount < minimum_withdrawal_amount {
        return Err(WithdrawalError::AmountTooLow {
            min_withdrawal_amount: minimum_withdrawal_amount.into(),
        });
    }
```

**File:** rs/ethereum/cketh/minter/src/endpoints.rs (L236-244)
```rust
            LedgerBurnError::AmountTooLow {
                minimum_burn_amount,
                failed_burn_amount,
                ledger,
            } => {
                panic!(
                    "BUG: withdrawal amount {failed_burn_amount} on the ckETH ledger {ledger:?} should always be higher than the ledger transaction fee {minimum_burn_amount}"
                )
            }
```

**File:** rs/ethereum/cketh/mainnet/minter_upgrade_2026_05_29.md (L21-24)
```markdown
* Reduce the minimum ETH withdrawal amount by a factor of 6, from 0.03 ETH (`30_000_000_000_000_000` wei) to 0.005 ETH (`5_000_000_000_000_000` wei) — approximately $10 at current prices. The reasoning is as follows:
    * The current minimum dates back to December 2023, when the ckETH minter was installed (see proposal [126171](https://dashboard.internetcomputer.org/proposal/126171)). At that time ETH traded in a similar USD range (around $2000), but Ethereum mainnet transaction fees were averaging $5–$10 per transaction ([source](https://bitinfocharts.com/comparison/ethereum-transactionfees.html#3y)).
    * Today, Ethereum mainnet fees are in the order of cents and rarely exceed $1.
    * As explained [here](https://github.com/dfinity/ic/blob/14382b5abb14b8e7de2bd4a3fb402ba069b82861/rs/ethereum/cketh/docs/cketh.adoc?plain=1#L208), an order-of-magnitude safety margin is preserved so the minter can always submit the transaction even when the Ethereum network is congested and one or more resubmissions are needed (each resubmission requires at least a 10% fee bump). With current Ethereum fees of ~$0.10–$1, a $10 minimum still preserves the ~10× safety margin even after several fee bumps.
```
