### Title
ckETH `withdraw_eth` Burns User Funds Before Fee Is Known With No User-Controlled Fee Cap - (File: rs/ethereum/cketh/minter/src/main.rs)

### Summary
The `withdraw_eth` endpoint in the ckETH minter burns the caller's ckETH immediately upon request, before the Ethereum transaction fee is estimated. The user has no way to specify a maximum acceptable fee. The fee is deducted asynchronously from the withdrawal amount by the timer-driven `create_transactions_batch`, and if gas prices spike between the burn and transaction creation, the user receives significantly less ETH than expected with no ability to cancel or bound the loss.

### Finding Description

The `withdraw_eth` endpoint accepts only `amount`, `recipient`, and `from_subaccount` — there is no `max_fee` or `max_transaction_fee` parameter. [1](#0-0) 

The handler immediately burns the full `amount` of ckETH from the user's account: [2](#0-1) 

The resulting `EthWithdrawalRequest` struct stores no `max_transaction_fee` field — only `withdrawal_amount`: [3](#0-2) 

Later, the timer-driven `create_transactions_batch` estimates the current gas fee and deducts it from the withdrawal amount using `ResubmissionStrategy::ReduceEthAmount`: [4](#0-3) 

The resubmission strategy for ckETH withdrawals reduces the ETH amount sent to the destination as fees increase, with no floor set by the user: [5](#0-4) 

This is in direct contrast to `Erc20WithdrawalRequest`, which carries a user-visible `max_transaction_fee` field that bounds the fee: [6](#0-5) 

The documentation explicitly acknowledges the unbounded fee deduction for ckETH withdrawals: [7](#0-6) 

### Impact Explanation

A user calling `withdraw_eth` has their ckETH burned immediately and irrevocably. The actual ETH fee deducted is determined by the Ethereum gas price at the time the minter's timer processes the request — which can be minutes to hours later. During an Ethereum gas price spike, the user could receive substantially less ETH than anticipated. The user cannot cancel the withdrawal after the burn, cannot set a maximum acceptable fee, and cannot recover the difference. The `total_unspent_tx_fees` metric confirms that overcharged fees are never reimbursed for ckETH withdrawals. [8](#0-7) 

### Likelihood Explanation

Any unprivileged user calling `withdraw_eth` is affected. Ethereum gas price spikes are common during periods of network congestion. The minter processes requests up to 6 minutes after submission (and longer during congestion), creating a window where the fee can diverge significantly from what the user observed via `estimate_withdrawal_fee` or `eip_1559_transaction_price` before submitting. This is a realistic, regularly occurring condition on Ethereum mainnet.

### Recommendation

Add an optional `max_transaction_fee : opt nat` field to `WithdrawalArg` (mirroring the existing `Erc20WithdrawalRequest.max_transaction_fee`). Store it in `EthWithdrawalRequest`. In `create_transaction` for the `CkEth` branch, reject the transaction with `CreateTransactionError::InsufficientTransactionFee` if the estimated fee exceeds the user-supplied cap, and trigger reimbursement of the burned ckETH. This matches the protection already implemented for `withdraw_erc20`. [9](#0-8) 

### Proof of Concept

1. User observes `estimate_withdrawal_fee` returning a low fee (e.g., 0.001 ETH) and calls:
   ```
   withdraw_eth(record { amount = 1_000_000_000_000_000_000; recipient = "0x..." })
   ```
2. The minter immediately burns `1.0 ckETH` from the user's account (line 301–312 of `main.rs`).
3. Ethereum gas prices spike 10× before the minter's timer fires.
4. `create_transactions_batch` calls `create_transaction` with the new high gas fee estimate, computing `tx_amount = withdrawal_amount - max_transaction_fee` where `max_transaction_fee` is now 0.01 ETH.
5. The user receives only `0.99 ETH` instead of the expected `0.999 ETH`, with no recourse — the `ResubmissionStrategy::ReduceEthAmount` will continue reducing the destination amount on each resubmission if fees keep rising.
6. The `total_unspent_tx_fees` counter increases but no reimbursement is issued for ckETH withdrawals.

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

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L121-142)
```rust
/// Ethereum withdrawal request issued by the user.
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

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L144-177)
```rust
/// ERC-20 withdrawal request issued by the user.
#[derive(Clone, Eq, PartialEq, Decode, Encode)]
pub struct Erc20WithdrawalRequest {
    /// Amount of burn ckETH that can be used to pay for the Ethereum transaction fees.
    #[n(0)]
    pub max_transaction_fee: Wei,
    /// The ERC-20 amount that the receiver will get.
    #[n(1)]
    pub withdrawal_amount: Erc20Value,
    /// The recipient's address of the sent ERC-20 tokens.
    #[n(2)]
    pub destination: Address,
    /// The transaction ID of the ckETH burn operation on the ckETH ledger.
    #[cbor(n(3), with = "crate::cbor::id")]
    pub cketh_ledger_burn_index: LedgerBurnIndex,
    /// Address of the ERC-20 smart contract that is the message call's recipient.
    #[n(4)]
    pub erc20_contract_address: Address,
    /// The ckERC20 ledger on which the minter burned the ckERC20 tokens.
    #[cbor(n(5), with = "icrc_cbor::principal")]
    pub ckerc20_ledger_id: Principal,
    /// The transaction ID of the ckERC20 burn operation on the ckERC20 ledger.
    #[cbor(n(6), with = "crate::cbor::id")]
    pub ckerc20_ledger_burn_index: LedgerBurnIndex,
    /// The owner of the account from which the minter burned ckETH.
    #[cbor(n(7), with = "icrc_cbor::principal")]
    pub from: Principal,
    /// The subaccount from which the minter burned ckETH.
    #[n(8)]
    pub from_subaccount: Option<LedgerSubaccount>,
    /// The IC time at which the withdrawal request arrived.
    #[n(9)]
    pub created_at: u64,
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

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1147-1168)
```rust
        WithdrawalRequest::CkErc20(request) => {
            // The transaction fee is already paid and must be at most
            // the `max_transaction_fee` in the withdrawal request, which, given a gas limit, gives us an upper bound on
            // the `max_fee_per_gas`. We allocate the maximum from the beginning to minimize
            // transaction resubmissions: even if the `base_fee_per_gas` increases considerably,
            // the transaction could still make it as long as `transaction.max_fee_per_gas >=  block.base_fee_per_gas`,
            // since the `priority_fee_per_gas` received by the miner is capped to (see https://eips.ethereum.org/EIPS/eip-1559)
            // min(transaction.max_priority_fee_per_gas, transaction.max_fee_per_gas - block.base_fee_per_gas).
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

**File:** rs/ethereum/cketh/minter/src/tx.rs (L131-144)
```rust
pub enum ResubmissionStrategy {
    ReduceEthAmount { withdrawal_amount: Wei },
    GuaranteeEthAmount { allowed_max_transaction_fee: Wei },
}

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

**File:** rs/ethereum/cketh/docs/cketh.adoc (L198-214)
```text
=== Cost of a withdrawal

Note that the transaction will be made at the cost of the beneficiary meaning that the resulting received amount will be less than the specified withdrawal amount.
The exact fee deducted depends on the dynamic Ethereum transaction fees used at the time the transaction was created.

In more detail, assume that a user calls `withdraw_eth` (after having approved the minter) to withdraw `withdraw_amount` (e.g. 1ckETH) to some address.
Then the minter is going to do the following

. Burn `withdraw_amount` on the ckETH ledger for the IC principal (the caller of `withdraw_eth`).
. Estimate the maximum current cost of a transaction on Ethereum, say `max_tx_fee_estimate`. This `max_tx_fee_estimate` is expected to be large enough to be valid for the few next blocks.
. Issue an Ethereum transaction (via threshold ECDSA) with the value `withdraw_amount - max_tx_fee_estimate`. This requires of course that `withdraw_amount >= max_tx_fee_estimate` and that's why we currently have a conservative minimum value for withdrawals of `30_000_000_000_000_000` wei. This ensures that the minter can always send the transaction to Ethereum if one or several resubmissions are needed if the Ethereum network is congested and fees are increasing rapidly (each resubmission requires an increase of at least 10% of the transaction fee).
. When the transaction is mined, the destination of the transaction will receive `withdraw_amount - max_tx_fee_estimate`. Since on Ethereum transactions are paid by the sender, the minter’s account will be charged with
+
----
(withdraw_amount - max_tx_fee_estimate) + actual_tx_fee == withdrawal_amount - (max_tx_fee_estimate - actual_tx_fee),
----
where `actual_tx_fee` represents the actual transaction fee (can be retrieved from the transaction receipt) and by construction `max_tx_fee_estimate - actual_tx_fee > 0`.
```

**File:** rs/ethereum/cketh/docs/cketh.adoc (L216-223)
```text
[TIP]
.Effective transaction fees vs unspent transaction fees
====
The minter dashboard displays in the metadata table the following fees

. `Total effective transaction fees`: the sum of all `actual_tx_fee` for all withdrawals.
. `Total unspent transaction fees`: the sum of all `max_tx_fee_estimate - actual_tx_fee` for all withdrawals. This represents an overestimate of the actual transaction fees that were charged to the user but in retrospect not needed to mine the sent transaction.
====
```
