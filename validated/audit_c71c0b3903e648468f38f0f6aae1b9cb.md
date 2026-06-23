### Title
Missing User-Specified Maximum Transaction Fee in ckETH `withdraw_eth` Exposes Users to Unbounded Gas Cost Deductions - (File: rs/ethereum/cketh/minter/src/main.rs)

### Summary
The ckETH minter's `withdraw_eth` endpoint burns the user's ckETH immediately upon call, but defers Ethereum transaction creation to an asynchronous processing loop that uses the gas fee estimate current at processing time. Because `WithdrawalArg` and `EthWithdrawalRequest` contain no `max_transaction_fee` field, users have no way to bound the fee deducted from their withdrawal. The ckERC20 path (`Erc20WithdrawalRequest`) already has this protection; the ckETH path does not.

### Finding Description

`WithdrawalArg` (the Candid input type for `withdraw_eth`) accepts only `amount`, `recipient`, and `from_subaccount`: [1](#0-0) 

This maps to `EthWithdrawalRequest`, which stores no fee cap: [2](#0-1) 

By contrast, `Erc20WithdrawalRequest` carries an explicit `max_transaction_fee` field: [3](#0-2) 

In `withdraw_eth`, the ckETH burn happens immediately and unconditionally before any fee check: [4](#0-3) 

Later, in the asynchronous `create_transactions_batch` loop, `create_transaction` is called with the gas fee estimate current at that moment. For the `CkEth` branch, the estimate is used directly with no user-supplied upper bound: [5](#0-4) 

The gas fee estimate itself is `2 * base_fee_per_gas + max_priority_fee_per_gas`, derived from the live Ethereum fee history at processing time: [6](#0-5) 

The `CkErc20` branch enforces the user's cap and returns `InsufficientTransactionFee` if the current fee exceeds it: [7](#0-6) 

No equivalent guard exists for `CkEth`.

### Impact Explanation

A user calls `withdraw_eth` expecting fees based on current gas prices. Their ckETH is burned immediately. The minter processes the withdrawal asynchronously (up to ~6 minutes later per documentation). If Ethereum gas prices spike in that window, the minter deducts a much larger fee from the withdrawal amount, and the user receives significantly less ETH than anticipated. The burn is irreversible; there is no cancellation path. The user's loss is bounded only by `withdrawal_amount - 1 wei` (the minimum non-zero tx amount), meaning in extreme congestion the user could receive near-zero ETH for a large ckETH burn. [8](#0-7) 

### Likelihood Explanation

Any unprivileged IC principal can call `withdraw_eth` via ingress. Ethereum gas price spikes of 5–10× within minutes are historically common (NFT drops, DeFi liquidation cascades). The minter's own documentation acknowledges that "additional delays may occasionally occur due to reasons such as congestion on the Ethereum network," which is precisely the condition that amplifies this risk. No special attacker capability is required; the harm occurs through normal usage. [9](#0-8) 

### Recommendation

- **Short term**: Add an optional `max_transaction_fee: opt nat` field to `WithdrawalArg` and `EthWithdrawalRequest`. In `create_transaction`'s `CkEth` branch, if the field is set and `max_transaction_fee > request.max_transaction_fee`, return `CreateTransactionError::InsufficientTransactionFee` (mirroring the existing `CkErc20` logic) and reschedule the request rather than processing it at an unacceptable fee.
- **Long term**: Align `EthWithdrawalRequest` with `Erc20WithdrawalRequest` so both carry a mandatory fee cap, and add integration tests covering gas-spike scenarios for the ckETH withdrawal path.

### Proof of Concept

1. Alice calls `withdraw_eth` with `amount = 0.1 ckETH (100_000_000_000_000_000 wei)` when Ethereum base fee is 10 gwei. Expected fee: `21_000 * 20_000_000_000 = 420_000_000_000_000 wei (~0.00042 ETH)`.
2. ckETH ledger burns `100_000_000_000_000_000 wei` from Alice immediately.
3. A popular NFT mint causes base fee to spike to 200 gwei before the minter's next processing cycle.
4. `lazy_refresh_gas_fee_estimate` fetches the new fee history; `estimate_transaction_fee` returns `base_fee_per_gas = 200 gwei`.
5. `create_transaction` computes `max_fee_per_gas = 2 * 200 gwei + 1.5 gwei = 401.5 gwei`; `max_transaction_fee = 21_000 * 401_500_000_000 = 8_431_500_000_000_000 wei (~0.0084 ETH)`.
6. Alice receives `100_000_000_000_000_000 - 8_431_500_000_000_000 = 91_568_500_000_000_000 wei` — a 20× larger fee than expected, with no recourse. [10](#0-9)

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

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L122-142)
```rust
#[derive(Clone, Eq, PartialEq, Decode, Encode)]
pub struct EthWithdrawalRequest {
    /// The ETH amount that the receiver will get, not accounting for the Ethereum transaction fees.
    #[n(0)]
    pub withdrawal_amount: Wei,
    /// The address to which the minter will send ETH.
    #[n(1)]
    pub destination: Address,
    /// The transaction ID of the ckETH burn operation.
    #[cbor(n(2), with = "crate::cbor::id")]
    pub ledger_burn_index: LedgerBurnIndex,
    /// The owner of the account from which the minter burned ckETH.
    #[cbor(n(3), with = "icrc_cbor::principal")]
    pub from: Principal,
    /// The subaccount from which the minter burned ckETH.
    #[n(4)]
    pub from_subaccount: Option<LedgerSubaccount>,
    /// The IC time at which the withdrawal request arrived.
    #[n(5)]
    pub created_at: Option<u64>,
}
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L144-150)
```rust
/// ERC-20 withdrawal request issued by the user.
#[derive(Clone, Eq, PartialEq, Decode, Encode)]
pub struct Erc20WithdrawalRequest {
    /// Amount of burn ckETH that can be used to pay for the Ethereum transaction fees.
    #[n(0)]
    pub max_transaction_fee: Wei,
    /// The ERC-20 amount that the receiver will get.
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1121-1145)
```rust
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

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1155-1168)
```rust
            let request_max_fee_per_gas = request
                .max_transaction_fee
                .into_wei_per_gas(gas_limit)
                .expect("BUG: gas_limit should be non-zero");
            let actual_min_max_fee_per_gas = gas_fee_estimate.min_max_fee_per_gas();
            if actual_min_max_fee_per_gas > request_max_fee_per_gas {
                return Err(CreateTransactionError::InsufficientTransactionFee {
                    cketh_ledger_burn_index: request.cketh_ledger_burn_index,
                    allowed_max_transaction_fee: request.max_transaction_fee,
                    actual_max_transaction_fee: actual_min_max_fee_per_gas
                        .transaction_cost(gas_limit)
                        .unwrap_or(Wei::MAX),
                });
            }
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L301-336)
```rust
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
```

**File:** rs/ethereum/cketh/minter/src/tx.rs (L517-536)
```rust
    pub fn checked_estimate_max_fee_per_gas(&self) -> Option<WeiPerGas> {
        self.base_fee_per_gas
            .checked_mul(2_u8)
            .and_then(|base_fee_estimate| {
                base_fee_estimate.checked_add(self.max_priority_fee_per_gas)
            })
    }

    pub fn estimate_max_fee_per_gas(&self) -> WeiPerGas {
        self.checked_estimate_max_fee_per_gas()
            .unwrap_or(WeiPerGas::MAX)
    }

    pub fn to_price(self, gas_limit: GasAmount) -> TransactionPrice {
        TransactionPrice {
            gas_limit,
            max_fee_per_gas: self.estimate_max_fee_per_gas(),
            max_priority_fee_per_gas: self.max_priority_fee_per_gas,
        }
    }
```

**File:** rs/ethereum/cketh/minter/src/tx.rs (L693-733)
```rust
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
    if gas_fee_estimate
        .checked_estimate_max_fee_per_gas()
        .is_none()
    {
        return Err(TransactionFeeEstimationError::Overflow(
            "max_fee_per_gas overflowed".to_string(),
        ));
    }
    Ok(gas_fee_estimate)
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
