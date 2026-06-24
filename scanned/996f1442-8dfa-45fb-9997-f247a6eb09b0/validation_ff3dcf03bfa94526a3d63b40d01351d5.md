### Title
ckETH `withdraw_eth` Lacks User-Supplied Maximum Fee Parameter, Exposing Users to Unbounded Gas Fee Deductions - (File: rs/ethereum/cketh/minter/src/main.rs)

### Summary

The `withdraw_eth` endpoint in the ckETH minter burns the user's full `amount` of ckETH immediately at call time, but deducts the Ethereum transaction fee asynchronously at transaction-creation time. Because `WithdrawalArg` accepts no `max_fee` or `min_received_amount` parameter, users have no on-chain protection against receiving significantly less ETH than expected when gas fees spike between withdrawal submission and transaction creation. The `withdraw_erc20` path does not share this flaw — it estimates and burns the fee at call time.

### Finding Description

`withdraw_eth` in `rs/ethereum/cketh/minter/src/main.rs` accepts only `amount`, `recipient`, and `from_subaccount`:

```
type WithdrawalArg = record {
    recipient : text;
    amount : nat;
    from_subaccount : opt Subaccount;
};
``` [1](#0-0) 

The function immediately burns the full `amount` from the user's ckETH ledger account: [2](#0-1) 

The withdrawal request is then queued. Later, during the minter's processing cycle, `create_transaction` is called. It estimates the current `max_transaction_fee` from the live Ethereum fee history and deducts it from `withdrawal_amount`: [3](#0-2) 

The fee formula doubles the next-block base fee: `max_fee_per_gas = 2 * base_fee_per_gas + max_priority_fee_per_gas`: [4](#0-3) 

The gas fee estimate is cached for up to 60 seconds and refreshed on demand: [5](#0-4) 

The user has no mechanism to bound the fee they are willing to pay. There is no `max_fee`, `min_received_amount`, or equivalent field in `WithdrawalArg`. The burn is irreversible; there is no `cancel_withdrawal` endpoint.

**Contrast with `withdraw_erc20`:** that path calls `estimate_erc20_transaction_fee()` synchronously inside the update call and burns the fee immediately, so the user knows the exact fee before committing: [6](#0-5) 

The documentation explicitly acknowledges the deferred-fee design for `withdraw_eth`: [7](#0-6) 

### Impact Explanation

A user who calls `withdraw_eth(amount = X)` irrevocably burns `X` ckETH. If Ethereum base fees spike between the moment of the burn and the moment `create_transaction` runs (up to minutes later), the minter deducts a much larger `max_transaction_fee` from `X`, and the user receives `X − max_transaction_fee` ETH — potentially a significant fraction less than expected. The user has no recourse: the burn is final, there is no cancellation path, and no user-supplied fee cap was checked. At the current minimum withdrawal of 0.005 ETH, a fee spike to even 0.002–0.003 ETH (well within historical Ethereum congestion ranges) would consume 40–60% of the minimum withdrawal amount. For users withdrawing near the minimum, this constitutes a material, unprotected loss of funds.

### Likelihood Explanation

Ethereum gas fees are volatile and can spike by an order of magnitude within minutes during network congestion events (NFT launches, liquidation cascades, etc.). The minter processes withdrawals within approximately 6 minutes. The `lazy_refresh_gas_fee_estimate` cache is up to 60 seconds stale. Any unprivileged IC principal can call `withdraw_eth` — no special role is required. The scenario requires only normal Ethereum network congestion, which is a recurring real-world condition, not a theoretical one.

### Recommendation

Add an optional `max_fee` (or `min_received_amount`) field to `WithdrawalArg`, analogous to how `withdraw_erc20` already estimates and locks in the fee at call time. If the fee at transaction-creation time exceeds the user-supplied cap, the minter should reimburse the user (minus the ledger transfer fee) rather than silently deducting an unexpectedly large amount. Alternatively, mirror the `withdraw_erc20` design: estimate the fee synchronously inside `withdraw_eth` and burn only `amount − estimated_fee` from the user, returning an error if the fee exceeds a user-supplied threshold.

### Proof of Concept

1. User queries `eip_1559_transaction_price` (query, no cost) and sees `max_transaction_fee = 0.0001 ETH`.
2. User calls `withdraw_eth(amount = 0.005 ETH, recipient = "0x...")`. The minter immediately burns 0.005 ckETH from the user's ledger account.
3. Before the minter's next processing cycle (~6 minutes), Ethereum base fees spike 20×.
4. `create_transaction` runs, calls `lazy_refresh_gas_fee_estimate`, and computes `max_transaction_fee = 0.002 ETH`.
5. The user receives `0.005 − 0.002 = 0.003 ETH` — 40% less than the 0.005 ETH they burned.
6. No error is returned; the withdrawal succeeds from the minter's perspective.
7. The user has no way to have prevented this: `WithdrawalArg` has no `max_fee` field, and there is no cancellation endpoint. [8](#0-7) [9](#0-8)

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

**File:** rs/ethereum/cketh/minter/src/main.rs (L430-432)
```rust
    let erc20_tx_fee = estimate_erc20_transaction_fee().await.ok_or_else(|| {
        WithdrawErc20Error::TemporarilyUnavailable("Failed to retrieve current gas fee".to_string())
    })?;
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1110-1145)
```rust
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

**File:** rs/ethereum/cketh/minter/src/tx.rs (L610-681)
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
}
```

**File:** rs/ethereum/cketh/minter/src/tx.rs (L688-724)
```rust
/// Estimate the transaction fee based on the fee history.
///
/// From the fee history, the current base fee per gas and the max priority fee per gas are determined.
/// Then, the max fee per gas is computed as `2 * base_fee_per_gas + max_priority_fee_per_gas` to ensure that
/// the estimate remains valid for the next few blocks, see `<https://www.blocknative.com/blog/eip-1559-fees>`.
pub fn estimate_transaction_fee(
    fee_history: &FeeHistory,
) -> Result<GasFeeEstimate, TransactionFeeEstimationError> {
    // average value between the `minSuggestedMaxPriorityFeePerGas`
    // used by Metamask, see
    // https://github.com/MetaMask/core/blob/f5a4f52e17f407c6411e4ef9bd6685aab184b91d/packages/gas-fee-controller/src/fetchGasEstimatesViaEthFeeHistory/calculateGasFeeEstimatesForPriorityLevels.ts#L14
    const MIN_MAX_PRIORITY_FEE_PER_GAS: WeiPerGas = WeiPerGas::new(1_500_000_000); //1.5 gwei
    let base_fee_per_gas_next_block = fee_history
        .base_fee_per_gas
        .last()
        .ok_or(TransactionFeeEstimationError::InvalidFeeHistory(
            "base_fee_per_gas should not be empty to be able to evaluate transaction price"
                .to_string(),
        ))?
        .clone();
    let max_priority_fee_per_gas = {
        let mut rewards: Vec<WeiPerGas> = fee_history
            .reward
            .iter()
            .flatten()
            .map(|nat| WeiPerGas::from(nat.clone()))
            .collect();
        let historic_max_priority_fee_per_gas =
            *median(&mut rewards).ok_or(TransactionFeeEstimationError::InvalidFeeHistory(
                "should be non-empty with rewards of the last 5 blocks".to_string(),
            ))?;
        historic_max_priority_fee_per_gas.max(MIN_MAX_PRIORITY_FEE_PER_GAS)
    };
    let gas_fee_estimate = GasFeeEstimate {
        base_fee_per_gas: base_fee_per_gas_next_block.into(),
        max_priority_fee_per_gas,
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
