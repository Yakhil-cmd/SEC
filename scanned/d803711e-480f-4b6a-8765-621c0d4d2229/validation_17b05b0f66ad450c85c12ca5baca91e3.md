### Title
ckETH `withdraw_eth` Lacks User-Controlled Maximum Fee — (`File: rs/ethereum/cketh/minter/src/main.rs`)

### Summary
The `withdraw_eth` endpoint on the ckETH minter burns the user's full `withdrawal_amount` immediately but defers the actual Ethereum transaction fee deduction to an asynchronous background task. Because `WithdrawalArg` accepts no `max_transaction_fee` parameter, users have no way to bound the fee that will be silently subtracted from their received ETH. If Ethereum gas prices spike between the `withdraw_eth` call and the minter's batch processing cycle (up to ~6 minutes later), the user receives materially less ETH than expected with no recourse. The analogous `withdraw_erc20` path already solves this correctly by accepting and enforcing a user-supplied `max_transaction_fee`.

### Finding Description

`WithdrawalArg`, the sole input type for `withdraw_eth`, contains only `amount`, `recipient`, and `from_subaccount`:

```
type WithdrawalArg = record {
    recipient : text;
    amount : nat;
    from_subaccount : opt Subaccount;
};
``` [1](#0-0) 

The corresponding internal struct `EthWithdrawalRequest` likewise carries no fee cap:

```rust
pub struct EthWithdrawalRequest {
    pub withdrawal_amount: Wei,
    pub destination: Address,
    pub ledger_burn_index: LedgerBurnIndex,
    pub from: Principal,
    pub from_subaccount: Option<LedgerSubaccount>,
    pub created_at: Option<u64>,
}
``` [2](#0-1) 

In `withdraw_eth`, the full `amount` is burned from the user's ckETH ledger account immediately and synchronously, before any fee is known: [3](#0-2) 

The withdrawal request is then queued. Later, the background task `create_transactions_batch` picks it up and calls `create_transaction`, which computes the fee from the **current** gas estimate at that moment and silently deducts it from the user's amount:

```rust
WithdrawalRequest::CkEth(request) => {
    let transaction_price = gas_fee_estimate.to_price(gas_limit);
    let max_transaction_fee = transaction_price.max_transaction_fee();
    let tx_amount = match request.withdrawal_amount.checked_sub(max_transaction_fee) {
        Some(tx_amount) => tx_amount,
        None => { return Err(CreateTransactionError::InsufficientTransactionFee { ... }); }
    };
    ...
}
``` [4](#0-3) 

The batch processor runs asynchronously and the documentation explicitly states the delay can be up to ~6 minutes: [5](#0-4) 

By contrast, `Erc20WithdrawalRequest` carries a `max_transaction_fee` field set at call time: [6](#0-5) 

And `create_transaction` for the ERC-20 path enforces it — returning an error if the current gas fee exceeds the user's cap: [7](#0-6) 

Additionally, the `ResubmissionStrategy` for ckETH is `ReduceEthAmount`, meaning every resubmission due to rising gas prices further reduces the ETH amount the user receives, with no floor set by the user: [8](#0-7) 

### Impact Explanation

A user who calls `withdraw_eth` with `amount = X` and observes a current fee of `F` at call time expects to receive approximately `X - F` ETH. If Ethereum gas prices spike during the ~6-minute processing window, the minter silently deducts a much larger fee `F'` and the user receives `X - F'` with no notification and no ability to cancel. The ckETH was already burned at call time, so the user cannot recover the difference. On resubmission cycles, the deduction compounds further via `ReduceEthAmount`. The user has no mechanism to express "do not proceed if the fee exceeds Y."

### Likelihood Explanation

Ethereum gas prices are well-documented to spike sharply within minutes during periods of network congestion (NFT mints, DeFi liquidations, etc.). The minter's documented processing delay of up to ~6 minutes, combined with the absence of any user-side fee cap, makes this a realistic scenario for any user withdrawing ckETH during volatile market conditions. No privileged access, admin key, or majority attack is required — any unprivileged user calling `withdraw_eth` is exposed.

### Recommendation

Add an optional `max_transaction_fee : opt nat` field to `WithdrawalArg` and propagate it into `EthWithdrawalRequest`. In `create_transaction`, when the field is set, reject the request (and trigger reimbursement) if the current gas fee estimate exceeds the user-supplied cap — mirroring the existing behavior for `WithdrawalRequest::CkErc20`. This is the pattern already implemented and working correctly in the `withdraw_erc20` path.

### Proof of Concept

1. User calls `withdraw_eth` with `amount = 0.1 ckETH` when the current gas fee is `0.001 ETH`. The minter burns `0.1 ckETH` immediately.
2. Ethereum gas prices spike 10× during the next 6 minutes.
3. `create_transactions_batch` runs, calls `create_transaction` with the new gas fee estimate of `0.01 ETH`.
4. The user receives `0.1 - 0.01 = 0.09 ETH` instead of the expected `~0.099 ETH` — a 10× larger fee deduction than anticipated, with no recourse.
5. If the transaction is not mined and must be resubmitted with a further 10% fee increase (`ResubmissionStrategy::ReduceEthAmount`), the received amount decreases again.
6. The user had no way to specify "abort if fee > 0.002 ETH" as they can with `withdraw_erc20` via `max_transaction_fee`.

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

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L530-537)
```rust
            resubmission: match &withdrawal_request {
                WithdrawalRequest::CkEth(cketh) => ResubmissionStrategy::ReduceEthAmount {
                    withdrawal_amount: cketh.withdrawal_amount,
                },
                WithdrawalRequest::CkErc20(ckerc20) => ResubmissionStrategy::GuaranteeEthAmount {
                    allowed_max_transaction_fee: ckerc20.max_transaction_fee,
                },
            },
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

**File:** rs/ethereum/cketh/minter/src/main.rs (L300-322)
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
        Ok(ledger_burn_index) => {
            let withdrawal_request = EthWithdrawalRequest {
                withdrawal_amount: amount,
                destination,
                ledger_burn_index,
                from: caller,
                from_subaccount: from_subaccount.and_then(LedgerSubaccount::from_bytes),
                created_at: Some(now),
            };
```

**File:** rs/ethereum/cketh/docs/cketh.adoc (L178-208)
```text
After calling `withdraw_eth`, the minter will usually send a transaction to the Ethereum network within 6 minutes. Additional delays may occasionally occur due to reasons such as congestion on the Ethereum network or some Ethereum JSON-RPC providers being offline.

=== Example of a withdrawal

.Approve the minter to spend 1 ETH (`1_000_000_000_000_000_000` wei)
====
[source,shell]
----
dfx canister --network ic call ledger icrc2_approve "(record { spender = record { owner = principal \"$(dfx canister id minter --network ic)\" }; amount = 1_000_000_000_000_000_000 })"
----
====

.Withdraw 0.15 ETH (`150_000_000_000_000_000` wei) to `0xAB586458E47f3e9D350e476fB7E294a57825A3f4`
====
[source,shell]
----
dfx canister --network ic call minter withdraw_eth "(record {amount = 150_000_000_000_000_000; recipient = \"0xAB586458E47f3e9D350e476fB7E294a57825A3f4\"})"
----
====

=== Cost of a withdrawal

Note that the transaction will be made at the cost of the beneficiary meaning that the resulting received amount will be less than the specified withdrawal amount.
The exact fee deducted depends on the dynamic Ethereum transaction fees used at the time the transaction was created.

In more detail, assume that a user calls `withdraw_eth` (after having approved the minter) to withdraw `withdraw_amount` (e.g. 1ckETH) to some address.
Then the minter is going to do the following

. Burn `withdraw_amount` on the ckETH ledger for the IC principal (the caller of `withdraw_eth`).
. Estimate the maximum current cost of a transaction on Ethereum, say `max_tx_fee_estimate`. This `max_tx_fee_estimate` is expected to be large enough to be valid for the few next blocks.
. Issue an Ethereum transaction (via threshold ECDSA) with the value `withdraw_amount - max_tx_fee_estimate`. This requires of course that `withdraw_amount >= max_tx_fee_estimate` and that's why we currently have a conservative minimum value for withdrawals of `30_000_000_000_000_000` wei. This ensures that the minter can always send the transaction to Ethereum if one or several resubmissions are needed if the Ethereum network is congested and fees are increasing rapidly (each resubmission requires an increase of at least 10% of the transaction fee).
```
