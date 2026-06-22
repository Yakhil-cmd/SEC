### Title
Missing Maximum Transaction Fee Parameter in `withdraw_eth` Allows Users to Receive Unexpectedly Low ETH Amounts - (File: rs/ethereum/cketh/minter/src/main.rs)

---

### Summary

The `withdraw_eth` endpoint in the ckETH minter burns the caller's ckETH immediately and irrevocably, but defers gas fee estimation to an asynchronous transaction-creation step that runs up to ~6 minutes later. Because `WithdrawalArg` exposes no `max_fee` or `min_received_amount` field, users have no way to bound the fee that will be deducted from their withdrawal. A gas-fee spike between the burn and the transaction creation silently reduces the ETH amount the user receives, with no recourse.

---

### Finding Description

`withdraw_eth` accepts only `{ recipient, amount, from_subaccount }`: [1](#0-0) 

Inside the handler, the full `amount` of ckETH is burned from the caller's account synchronously: [2](#0-1) 

The withdrawal is then queued with no fee information attached: [3](#0-2) 

The gas fee is only estimated later, inside `create_transaction`, which is called asynchronously by the minter's heartbeat/timer loop: [4](#0-3) 

The resulting ETH amount sent to the recipient is `withdrawal_amount - max_transaction_fee`, where `max_transaction_fee` is computed at transaction-creation time, not at call time. The minter's own documentation confirms this: [5](#0-4) 

The gas-fee estimate is cached for up to 60 seconds and refreshed via EVM RPC outcalls: [6](#0-5) 

This means the fee used to compute the actual ETH sent can differ substantially from any estimate the user queried before calling `withdraw_eth`. There is no parameter the user can supply to abort the withdrawal if the fee exceeds an acceptable threshold.

By contrast, `withdraw_erc20` does call `estimate_erc20_transaction_fee()` synchronously at call time before burning ckETH: [7](#0-6) 

Even so, `WithdrawErc20Arg` also lacks a `max_fee` field, so the same class of issue applies there, though the synchronous estimate reduces the exposure window.

The `treasury_manager.did` draft API explicitly calls out this pattern as a known security risk for liquidity-pool deposits: [8](#0-7) 

---

### Impact Explanation

A user who calls `withdraw_eth` with `amount = X` ckETH expects to receive approximately `X - current_fee` ETH. If Ethereum gas fees spike between the burn and the transaction creation (up to ~6 minutes later), the minter deducts a much larger fee, and the user receives far less ETH than anticipated. The ckETH is already burned and the withdrawal cannot be cancelled. The only existing guard is the global `minimum_withdrawal_amount` floor, which prevents the user from receiving zero ETH but does not protect against receiving an unexpectedly small amount. [9](#0-8) 

---

### Likelihood Explanation

Ethereum gas fees are volatile and can spike by an order of magnitude within minutes during high-demand events (NFT launches, DeFi liquidation cascades, etc.). The minter's processing window is documented as "usually within 6 minutes." Any unprivileged user who calls `withdraw_eth` during a low-fee period and whose request is processed during a high-fee spike is affected. No attacker action is required; normal market volatility is sufficient. The minter's own resubmission logic (each resubmission bumps fees by ≥10%) can compound the effect for queued withdrawals. [10](#0-9) 

---

### Recommendation

**Short term:** Add an optional `max_fee : opt nat` field to `WithdrawalArg`. Before burning ckETH, call `lazy_refresh_gas_fee_estimate()` and reject with a new `WithdrawalError::FeeTooHigh { estimated_fee }` variant if the estimated fee exceeds the caller-supplied cap. This mirrors the pattern already used in `withdraw_erc20` for the synchronous fee check.

**Long term:** For every chain-fusion endpoint that gives a user an output amount in exchange for an input burn, ensure the user can specify a minimum acceptable output (or maximum acceptable cost). State changes driven by external market data (Ethereum gas prices, ICP/XDR rates) outside the user's control can otherwise result in unexpectedly low output and a loss of funds with no recourse.

---

### Proof of Concept

1. Alice queries `eip_1559_transaction_price` and sees `max_transaction_fee = 0.001 ETH`.
2. Alice calls `withdraw_eth({ amount = 0.1 ETH, recipient = "0x..." })`.
3. The minter immediately burns 0.1 ckETH from Alice's account.
4. Before the minter's next processing cycle (~6 minutes), Ethereum gas fees spike 10×.
5. `create_transaction` is called with `max_transaction_fee = 0.01 ETH`.
6. Alice receives `0.1 - 0.01 = 0.09 ETH` instead of the expected `~0.099 ETH` — a 10× larger fee deduction than anticipated, with no way to have prevented it after step 2. [11](#0-10) [12](#0-11)

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

**File:** rs/ethereum/cketh/minter/src/tx.rs (L563-607)
```rust
    /// the transaction can be resubmitted (See [Retrying an EIP 1559 transaction](https://docs.alchemy.com/docs/retrying-an-eip-1559-transaction)).
    /// The current `max_fee_per_gas` will be kept as long as it is enough to cover the new `max_priority_fee_per_gas + base_fee_per_gas_next_block`.
    pub fn resubmit_transaction_price(self, new_gas_fee: GasFeeEstimate) -> Self {
        let plus_10_percent = |amount: WeiPerGas| {
            amount
                .checked_add(
                    amount
                        .checked_div_ceil(10_u8)
                        .expect("BUG: must be Some() because divisor is non-zero"),
                )
                .unwrap_or(WeiPerGas::MAX)
        };

        if self.max_fee_per_gas >= new_gas_fee.min_max_fee_per_gas()
            && self.max_priority_fee_per_gas >= new_gas_fee.max_priority_fee_per_gas
        {
            self
        } else {
            // At this point the transaction price needs to be updated
            // which involves a minimum increase of 10% in the max_priority_fee_per_gas.
            // We also need to ensure that the new max_fee_per_gas covers the new max_priority_fee_per_gas,
            // but it would be counter-productive to increase it further than the minimum required.
            // The reason is that any increase in the max_fee_per_gas may render the corresponding transaction
            // not resubmittable due to the user not having enough funds to cover the new transaction price,
            // which could potentially block the minter further. In other words, having a stuck transaction with a higher
            // max_priority_fee_per_gas, is better than having a stuck transaction with a lower max_priority_fee_per_gas,
            // since the first one will go through sooner than the second one when the transaction prices decrease.
            // In case of steep increasing transaction fees, several resubmissions each involving costly operations
            // (various HTTPs outcalls, tECDSA signatures, etc.) might be required, which potentially could be avoided,
            // if one were to increase the max_fee_per_gas more than the minimum required. However,
            // this seems less important than getting the minter unstuck as soon as possible.
            let updated_max_priority_fee_per_gas = plus_10_percent(self.max_priority_fee_per_gas)
                .max(new_gas_fee.max_priority_fee_per_gas);
            let new_gas_fee = GasFeeEstimate {
                max_priority_fee_per_gas: updated_max_priority_fee_per_gas,
                ..new_gas_fee
            };
            let new_max_fee_per_gas = new_gas_fee.min_max_fee_per_gas().max(self.max_fee_per_gas);
            TransactionPrice {
                gas_limit: self.gas_limit,
                max_fee_per_gas: new_max_fee_per_gas,
                max_priority_fee_per_gas: updated_max_priority_fee_per_gas,
            }
        }
    }
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

**File:** rs/sns/treasury_manager/treasury_manager.did (L35-40)
```text
// Known Security Risks:
// ====================
// Some liquidity pools do not implement slippage protection
// for deposits. In other words, the price ratio at the time
// of execution may differ from the ratio at the time the proposal was
// approved.
```
