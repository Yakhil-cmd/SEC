### Title
Missing Minimum Received Amount in `withdraw_eth` Allows Users to Receive Less ETH Than Expected - (File: rs/ethereum/cketh/minter/src/main.rs)

---

### Summary

The `withdraw_eth` endpoint in the ckETH minter burns the user's ckETH immediately upon call, but the actual ETH amount delivered to the recipient is determined asynchronously — up to ~6 minutes later — by subtracting a dynamically estimated gas fee. There is no `min_received_amount` parameter in `WithdrawalArg`, so users cannot protect themselves from receiving significantly less ETH than expected if Ethereum gas fees spike between submission and processing.

---

### Finding Description

The `withdraw_eth` function accepts a `WithdrawalArg` containing only `amount`, `recipient`, and `from_subaccount`: [1](#0-0) 

The function immediately burns the full `amount` of ckETH from the user's account: [2](#0-1) 

The withdrawal request is then queued. Later, when the minter's heartbeat processes the queue, `create_transaction` is called with the **current** gas fee estimate at that time. The actual ETH value sent to the recipient is computed as:

```
tx_amount = withdrawal_amount - max_transaction_fee
``` [3](#0-2) 

The documentation explicitly acknowledges this variable deduction: [4](#0-3) 

There is no field in `WithdrawalArg` and no check in `withdraw_eth` that enforces a minimum ETH amount the user is willing to receive. The `WithdrawalError` enum has no `SlippageExceeded` or `ReceivedAmountTooLow` variant: [5](#0-4) 

---

### Impact Explanation

**Impact: Medium.** A user who calls `withdraw_eth` expecting to receive approximately `amount - current_fee` ETH may receive substantially less if Ethereum gas fees spike between the time of the call and the time the minter creates the transaction (up to ~6 minutes later). The ckETH is already burned and non-refundable at that point. The user has no on-chain mechanism to abort or bound the loss.

The documentation notes that overcharged transaction fees are **not reimbursed**: [6](#0-5) 

---

### Likelihood Explanation

**Likelihood: Medium.** Ethereum gas fees are highly volatile and can spike by 10x or more within minutes during periods of network congestion. The minter processes withdrawals on a ~6-minute cycle. A user who queries `eip_1559_transaction_price` before calling `withdraw_eth` gets a stale estimate: [7](#0-6) 

The minter also applies a safety margin to the fee estimate to allow for resubmissions, meaning the user is always charged more than the actual fee paid. This is by design but compounds the unpredictability for the user.

---

### Recommendation

Add an optional `min_received_amount` field to `WithdrawalArg`:

```diff
type WithdrawalArg = record {
    recipient : text;
    amount : nat;
    from_subaccount : opt Subaccount;
+   min_received_amount : opt nat;
};
```

In `withdraw_eth`, after the burn succeeds and before queuing the request, store `min_received_amount` in `EthWithdrawalRequest`. In `create_transaction`, after computing `tx_amount = withdrawal_amount - max_transaction_fee`, check:

```rust
if let Some(min) = request.min_received_amount {
    if tx_amount < min {
        return Err(CreateTransactionError::ReceivedAmountBelowMinimum { ... });
    }
}
```

If the check fails, the withdrawal should be reimbursed (as already done for `InsufficientTransactionFee`). [8](#0-7) 

---

### Proof of Concept

1. Ethereum gas fees are currently low (e.g., `max_tx_fee_estimate = 0.001 ETH`).
2. User queries `eip_1559_transaction_price` and sees a low fee.
3. User calls `withdraw_eth({ amount: 0.1 ETH, recipient: "0x..." })`. ckETH is burned immediately.
4. Before the minter's next processing cycle (~6 minutes), Ethereum network congestion spikes gas fees 10x (e.g., `max_tx_fee_estimate = 0.01 ETH`).
5. `create_transaction` is called with the new fee estimate. `tx_amount = 0.1 - 0.01 = 0.09 ETH`.
6. User receives `0.09 ETH` instead of the `~0.099 ETH` they expected. The `0.009 ETH` overcharge is not reimbursed.
7. With no `min_received_amount` parameter, the user had no way to prevent this outcome. [1](#0-0) [9](#0-8)

### Citations

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L298-307)
```text
type WithdrawalArg = record {
    // The address to which the minter should deposit ETH.
    recipient : text;

    // The amount of ckETH in Wei that the client wants to withdraw.
    amount : nat;

    // The subaccount to burn ckETH from.
    from_subaccount : opt Subaccount;
};
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L169-197)
```rust
#[query]
async fn eip_1559_transaction_price(
    token: Option<Eip1559TransactionPriceArg>,
) -> Eip1559TransactionPrice {
    let gas_limit = match token {
        None => CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT,
        Some(Eip1559TransactionPriceArg { ckerc20_ledger_id }) => {
            match read_state(|s| s.find_ck_erc20_token_by_ledger_id(&ckerc20_ledger_id)) {
                Some(_) => CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT,
                None => {
                    if ckerc20_ledger_id == read_state(|s| s.cketh_ledger_id) {
                        CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT
                    } else {
                        ic_cdk::trap(format!(
                            "ERROR: Unsupported ckERC20 token ledger {ckerc20_ledger_id}"
                        ))
                    }
                }
            }
        }
    };
    match read_state(|s| s.last_transaction_price_estimate.clone()) {
        Some((ts, estimate)) => {
            let mut result = Eip1559TransactionPrice::from(estimate.to_price(gas_limit));
            result.timestamp = Some(ts);
            result
        }
        None => ic_cdk::trap("ERROR: last transaction price estimate is not available"),
    }
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L265-340)
```rust
#[update]
async fn withdraw_eth(
    WithdrawalArg {
        amount,
        recipient,
        from_subaccount,
    }: WithdrawalArg,
) -> Result<RetrieveEthRequest, WithdrawalError> {
    let caller = validate_caller_not_anonymous();
    let _guard = retrieve_withdraw_guard(caller).unwrap_or_else(|e| {
        ic_cdk::trap(format!(
            "Failed retrieving guard for principal {caller}: {e:?}"
        ))
    });

    let destination = validate_address_as_destination(&recipient).map_err(|e| match e {
        AddressValidationError::Invalid { .. } | AddressValidationError::NotSupported(_) => {
            ic_cdk::trap(e.to_string())
        }
        AddressValidationError::Blocked(address) => WithdrawalError::RecipientAddressBlocked {
            address: address.to_string(),
        },
    })?;

    let amount = Wei::try_from(amount).expect("failed to convert Nat to u256");

    let minimum_withdrawal_amount = read_state(|s| s.cketh_minimum_withdrawal_amount);
    if amount < minimum_withdrawal_amount {
        return Err(WithdrawalError::AmountTooLow {
            min_withdrawal_amount: minimum_withdrawal_amount.into(),
        });
    }

    let client = read_state(LedgerClient::cketh_ledger_from_state);
    let now = ic_cdk::api::time();
    log!(INFO, "[withdraw]: burning {:?}", amount);
    match client
        .burn_from(
            Account {
                owner: caller,
                subaccount: from_subaccount,
            },
            amount,
            BurnMemo::Convert {
                to_address: destination,
            },
        )
        .await
    {
        Ok(ledger_burn_index) => {
            let withdrawal_request = EthWithdrawalRequest {
                withdrawal_amount: amount,
                destination,
                ledger_burn_index,
                from: caller,
                from_subaccount: from_subaccount.and_then(LedgerSubaccount::from_bytes),
                created_at: Some(now),
            };

            log!(
                INFO,
                "[withdraw]: queuing withdrawal request {:?}",
                withdrawal_request,
            );

            mutate_state(|s| {
                process_event(
                    s,
                    EventType::AcceptedEthWithdrawalRequest(withdrawal_request.clone()),
                );
            });
            Ok(RetrieveEthRequest::from(withdrawal_request))
        }
        Err(e) => Err(WithdrawalError::from(e)),
    }
}
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1122-1134)
```rust
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

**File:** rs/ethereum/cketh/docs/cketh.adoc (L200-208)
```text
Note that the transaction will be made at the cost of the beneficiary meaning that the resulting received amount will be less than the specified withdrawal amount.
The exact fee deducted depends on the dynamic Ethereum transaction fees used at the time the transaction was created.

In more detail, assume that a user calls `withdraw_eth` (after having approved the minter) to withdraw `withdraw_amount` (e.g. 1ckETH) to some address.
Then the minter is going to do the following

. Burn `withdraw_amount` on the ckETH ledger for the IC principal (the caller of `withdraw_eth`).
. Estimate the maximum current cost of a transaction on Ethereum, say `max_tx_fee_estimate`. This `max_tx_fee_estimate` is expected to be large enough to be valid for the few next blocks.
. Issue an Ethereum transaction (via threshold ECDSA) with the value `withdraw_amount - max_tx_fee_estimate`. This requires of course that `withdraw_amount >= max_tx_fee_estimate` and that's why we currently have a conservative minimum value for withdrawals of `30_000_000_000_000_000` wei. This ensures that the minter can always send the transaction to Ethereum if one or several resubmissions are needed if the Ethereum network is congested and fees are increasing rapidly (each resubmission requires an increase of at least 10% of the transaction fee).
```

**File:** rs/ethereum/cketh/minter/src/endpoints.rs (L215-222)
```rust
#[derive(PartialEq, Debug, CandidType, Deserialize)]
pub enum WithdrawalError {
    AmountTooLow { min_withdrawal_amount: Nat },
    InsufficientFunds { balance: Nat },
    InsufficientAllowance { allowance: Nat },
    RecipientAddressBlocked { address: String },
    TemporarilyUnavailable(String),
}
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L275-275)
```text
. The minter retrieves the receipt of the finalized transaction (as done currently by the ckETH minter) and will reimburse the ckERC20 tokens in case the transaction failed. Overcharged transaction fees are not reimbursed.
```
