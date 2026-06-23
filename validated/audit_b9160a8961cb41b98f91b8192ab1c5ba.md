### Title
Lack of Minimum Received Amount and Deadline Parameters in `withdraw_eth` and `withdraw_erc20` - (`File: rs/ethereum/cketh/minter/src/main.rs`)

### Summary

The ckETH minter's `withdraw_eth` function burns ckETH from the user immediately at call time, but the actual ETH amount delivered on Ethereum is determined asynchronously later based on a dynamic gas fee estimate. No minimum received amount or deadline parameter is accepted. In `withdraw_erc20`, the ckETH gas fee is burned using a potentially stale (up to 60-second-old) estimate with no user-specified maximum. Users have no on-chain protection against receiving significantly less than expected.

### Finding Description

**`withdraw_eth` — deferred fee deduction with no slippage guard**

When a user calls `withdraw_eth`, the minter immediately burns the full `amount` of ckETH from the ledger: [1](#0-0) 

The withdrawal request is then queued. The actual gas fee (`max_tx_fee_estimate`) is only computed later, inside `create_transactions_batch`, when the background processing loop calls `create_transaction` with the current `GasFeeEstimate`: [2](#0-1) 

The ETH amount delivered to the user is `withdrawal_amount - max_transaction_fee`, computed at that later point: [3](#0-2) 

If gas prices spike between the time the user calls `withdraw_eth` and the time the transaction is created, the user receives substantially less ETH than they expected when they initiated the withdrawal. There is no `min_received_amount` or `deadline` field in `WithdrawalArg`.

If gas prices spike so severely that `withdrawal_amount < max_transaction_fee`, the request is **rescheduled to the back of the queue** indefinitely — the user's ckETH is already burned with no refund path until gas prices fall: [4](#0-3) 

**`withdraw_erc20` — stale gas fee estimate with no user-specified cap**

`withdraw_erc20` calls `estimate_erc20_transaction_fee()`, which internally calls `lazy_refresh_gas_fee_estimate()`. This function reuses a cached estimate if it is less than 60 seconds old: [5](#0-4) 

The ckETH gas fee is burned immediately based on this potentially stale estimate: [6](#0-5) 

The documentation explicitly states that "Overcharged transaction fees are not reimbursed": [7](#0-6) 

Neither `WithdrawalArg` (for ckETH) nor `WithdrawErc20Arg` (for ckERC20) exposes a `max_fee`, `min_received`, or `deadline` field in the Candid interface: [8](#0-7) 

### Impact Explanation

- **`withdraw_eth`**: A user who calls `withdraw_eth` with `amount = X` ckETH may receive significantly less than `X - expected_fee` ETH on Ethereum if gas prices rise between the call and the asynchronous transaction creation. In the worst case (gas spike exceeds the withdrawal amount), the user's ckETH is permanently burned while the withdrawal is rescheduled indefinitely with no cancellation or refund mechanism.
- **`withdraw_erc20`**: A user may be charged a ckETH gas fee based on a 60-second-old estimate that is lower than the actual fee at execution time. The overcharge is not reimbursed.
- Both cases result in direct, unrecoverable financial loss for the user with no on-chain recourse.

### Likelihood Explanation

Ethereum gas prices are well-known to spike sharply during periods of network congestion (e.g., NFT mints, DeFi liquidation cascades). The IC minter processes withdrawals asynchronously; a withdrawal request can sit in the queue for multiple minutes. During that window, gas prices can increase by multiples. The 60-second cache window for `withdraw_erc20` is a smaller but still real exposure window. Both scenarios are realistic for any active Ethereum network period.

### Recommendation

1. Add an optional `min_received_amount: opt nat` field to `WithdrawalArg` for `withdraw_eth`. Before queuing the withdrawal, check the current gas fee estimate and reject if `amount - current_max_fee_estimate < min_received_amount`.
2. Add an optional `max_fee: opt nat` field to `WithdrawErc20Arg` for `withdraw_erc20`. Reject the call if `estimate_erc20_transaction_fee()` exceeds `max_fee`.
3. Add an optional `deadline: opt nat64` (nanoseconds since Unix epoch) to both. In `create_transactions_batch`, skip and reimburse any queued request whose deadline has passed.
4. For the indefinite-reschedule case in `withdraw_eth`, introduce a maximum retry count or a deadline after which the burned ckETH is automatically reimbursed.

### Proof of Concept

**Scenario for `withdraw_eth`:**

1. Ethereum base fee is 10 gwei. User queries `eip_1559_transaction_price` and sees `max_transaction_fee ≈ 0.0003 ETH`.
2. User calls `withdraw_eth(amount = 0.005 ETH, recipient = "0x...")`. The minter immediately burns 0.005 ckETH from the ledger.
3. Before the minter's background loop processes the request, Ethereum gas prices spike 10× (e.g., during a major NFT mint). Base fee is now 100 gwei; `max_transaction_fee ≈ 0.003 ETH`.
4. `create_transactions_batch` runs. For the `CkEth` branch, `tx_amount = 0.005 - 0.003 = 0.002 ETH` — the user receives 0.002 ETH instead of the ~0.0047 ETH they expected. No minimum received amount check exists.
5. If gas spikes further so that `max_transaction_fee > 0.005 ETH`, `InsufficientTransactionFee` is returned and the request is rescheduled indefinitely. The user's 0.005 ckETH is burned with no refund. [9](#0-8) [4](#0-3)

### Citations

**File:** rs/ethereum/cketh/minter/src/main.rs (L300-313)
```rust
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
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L430-458)
```rust
    let erc20_tx_fee = estimate_erc20_transaction_fee().await.ok_or_else(|| {
        WithdrawErc20Error::TemporarilyUnavailable("Failed to retrieve current gas fee".to_string())
    })?;
    let cketh_account = Account {
        owner: caller,
        subaccount: from_cketh_subaccount,
    };
    let ckerc20_account = Account {
        owner: caller,
        subaccount: from_ckerc20_subaccount,
    };
    let now = ic_cdk::api::time();
    log!(
        INFO,
        "[withdraw_erc20]: burning {:?} ckETH from account {}",
        erc20_tx_fee,
        cketh_account
    );
    match cketh_ledger
        .burn_from(
            cketh_account,
            erc20_tx_fee,
            BurnMemo::Erc20GasFee {
                ckerc20_token_symbol: ckerc20_token.ckerc20_token_symbol.clone(),
                ckerc20_withdrawal_amount,
                to_address: destination,
            },
        )
        .await
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

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1122-1145)
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
            Ok(Eip1559TransactionRequest {
                chain_id: ethereum_network.chain_id(),
                nonce,
                max_priority_fee_per_gas: transaction_price.max_priority_fee_per_gas,
                max_fee_per_gas: transaction_price.max_fee_per_gas,
                gas_limit: transaction_price.gas_limit,
                destination: request.destination,
                amount: tx_amount,
                data: Vec::new(),
                access_list: Default::default(),
            })
```

**File:** rs/ethereum/cketh/minter/src/tx.rs (L610-680)
```rust
pub async fn lazy_refresh_gas_fee_estimate() -> Option<GasFeeEstimate> {
    const MAX_AGE_NS: u64 = 60_000_000_000_u64; //60 seconds

    async fn do_refresh() -> Option<GasFeeEstimate> {
        let _guard = match TimerGuard::new(TaskType::RefreshGasFeeEstimate) {
            Ok(guard) => guard,
            Err(e) => {
                log!(
                    DEBUG,
                    "[refresh_gas_fee_estimate]: Failed retrieving guard: {e:?}",
                );
                return None;
            }
        };

        let fee_history = match eth_fee_history().await {
            Ok(fee_history) => fee_history,
            Err(e) => {
                log!(
                    INFO,
                    "[refresh_gas_fee_estimate]: Failed retrieving fee history: {e:?}",
                );
                return None;
            }
        };

        let gas_fee_estimate = match estimate_transaction_fee(&fee_history) {
            Ok(estimate) => {
                mutate_state(|s| {
                    s.last_transaction_price_estimate =
                        Some((ic_cdk::api::time(), estimate.clone()));
                });
                estimate
            }
            Err(e) => {
                log!(
                    INFO,
                    "[refresh_gas_fee_estimate]: Failed estimating gas fee: {e:?}",
                );
                return None;
            }
        };
        log!(
            INFO,
            "[refresh_gas_fee_estimate]: Estimated transaction fee: {:?}",
            gas_fee_estimate,
        );
        Some(gas_fee_estimate)
    }

    async fn eth_fee_history() -> Result<FeeHistory, MultiCallError<FeeHistory>> {
        read_state(rpc_client)
            .fee_history((5_u8, BlockTag::Latest))
            .with_reward_percentiles(vec![20])
            .with_cycles(MIN_ATTACHED_CYCLES)
            .try_send()
            .await
            .reduce_with_strategy(StrictMajorityByKey::new(|fee_history: &FeeHistory| {
                Nat::from(fee_history.oldest_block.clone())
            }))
    }

    let now_ns = ic_cdk::api::time();
    match read_state(|s| s.last_transaction_price_estimate.clone()) {
        Some((last_estimate_timestamp_ns, estimate))
            if now_ns < last_estimate_timestamp_ns.saturating_add(MAX_AGE_NS) =>
        {
            Some(estimate)
        }
        _ => do_refresh().await,
    }
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L275-275)
```text
. The minter retrieves the receipt of the finalized transaction (as done currently by the ckETH minter) and will reimburse the ckERC20 tokens in case the transaction failed. Overcharged transaction fees are not reimbursed.
```

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L298-310)
```text
type WithdrawalArg = record {
    // The address to which the minter should deposit ETH.
    recipient : text;

    // The amount of ckETH in Wei that the client wants to withdraw.
    amount : nat;

    // The subaccount to burn ckETH from.
    from_subaccount : opt Subaccount;
};

// Details of a withdrawal request and its status.
type WithdrawalDetail = record {
```
