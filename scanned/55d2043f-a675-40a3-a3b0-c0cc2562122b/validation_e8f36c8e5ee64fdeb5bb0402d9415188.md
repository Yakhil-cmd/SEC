### Title
ckERC20 Withdrawal `max_transaction_fee` Locked at Request Time with No User Override — (`rs/ethereum/cketh/minter/src/main.rs`, `rs/ethereum/cketh/minter/src/state/transactions/mod.rs`)

---

### Summary

When a user calls `withdraw_erc20()` on the ckETH minter, the Ethereum gas fee is estimated at that instant and the corresponding ckETH amount is immediately burned. This burned amount is stored as the immutable `max_transaction_fee` field in `Erc20WithdrawalRequest`. There is no endpoint for the user to increase this cap after the request is accepted. If Ethereum gas prices rise above the locked cap before the transaction is created, the withdrawal is rescheduled indefinitely with no cancellation or fee top-up mechanism, leaving the user's ckERC20 tokens and ckETH fee permanently locked.

---

### Finding Description

In `withdraw_erc20()`, the minter calls `estimate_erc20_transaction_fee()` which internally calls `lazy_refresh_gas_fee_estimate()` (cached for up to 60 seconds) and computes `max_transaction_fee = gas_fee_estimate.to_price(CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT).max_transaction_fee()`. [1](#0-0) 

This fee is immediately burned from the user's ckETH balance: [2](#0-1) 

The burned amount is then stored as the immutable `max_transaction_fee` field in `Erc20WithdrawalRequest`: [3](#0-2) 

The struct definition confirms this field is fixed at request creation time with no update path: [4](#0-3) 

When the minter's timer fires and attempts to create the Ethereum transaction, `create_transaction()` checks whether the current gas fee exceeds the locked cap: [5](#0-4) 

If the current gas price exceeds `request_max_fee_per_gas`, the function returns `CreateTransactionError::InsufficientTransactionFee` and the request is rescheduled to the back of the queue: [6](#0-5) 

The minter's public interface (`.did` file) exposes no `cancel_withdrawal`, `update_max_fee`, or equivalent endpoint: [7](#0-6) 

The reimbursement logic only triggers on a finalized-but-failed Ethereum transaction, not on a stuck-pending request: [8](#0-7) 

---

### Impact Explanation

A user who calls `withdraw_erc20()` during a period of normal gas prices has their ckERC20 tokens and ckETH fee burned immediately and irrevocably. If Ethereum gas prices spike and remain elevated above the locked `max_transaction_fee`, the withdrawal request cycles indefinitely through `reschedule_withdrawal_request` with no forward progress. The user cannot:

1. Cancel the request and recover their ckERC20 tokens.
2. Top up the `max_transaction_fee` to accommodate higher gas prices.
3. Receive any reimbursement (reimbursement only applies to finalized-failed transactions, not stuck-pending ones).

The user's funds are locked in the minter for an unbounded duration, with recovery only possible if gas prices naturally fall back below the locked cap.

---

### Likelihood Explanation

Ethereum gas prices are volatile and can spike 3–10× within minutes during high-demand events (NFT mints, DeFi liquidation cascades, network congestion). The `lazy_refresh_gas_fee_estimate()` cache is up to 60 seconds stale at the time of the `withdraw_erc20()` call: [9](#0-8) 

The fee estimate formula (`2 * base_fee + priority_fee`) provides a buffer for the next few blocks but not for sustained spikes. Any user who calls `withdraw_erc20()` just before a gas spike — a realistic scenario — will have their withdrawal stuck. The issue is reachable by any unprivileged user with no special access required.

---

### Recommendation

1. **Add a `cancel_erc20_withdrawal` endpoint** that allows the original caller to cancel a pending (not yet signed/sent) withdrawal request and receive reimbursement of both the ckETH fee and the ckERC20 tokens.
2. **Add a `top_up_erc20_withdrawal_fee` endpoint** that allows the original caller to burn additional ckETH to increase the `max_transaction_fee` of a pending request, analogous to the `forceUpdateSlippage()` pattern in the referenced report.
3. Alternatively, extend the reimbursement logic to cover requests that have been stuck in the pending queue beyond a configurable timeout, automatically returning the ckERC20 tokens to the user.

---

### Proof of Concept

1. User queries `eip_1559_transaction_price` and observes `max_transaction_fee = X`.
2. User approves minter for `X` ckETH and calls `withdraw_erc20(amount, ckerc20_ledger_id, recipient)`.
3. Minter burns `X` ckETH and `amount` ckERC20 tokens; `Erc20WithdrawalRequest { max_transaction_fee: X, ... }` is enqueued.
4. Ethereum gas prices spike to `3X` before the minter's next timer tick.
5. `create_transactions_batch()` calls `create_transaction()` which evaluates `actual_min_max_fee_per_gas > request_max_fee_per_gas` → `true` → returns `InsufficientTransactionFee`.
6. Request is rescheduled to the back of the queue via `reschedule_withdrawal_request`.
7. Steps 5–6 repeat on every timer tick as long as gas prices remain elevated.
8. User has no endpoint to cancel, top up, or recover funds. Both the ckETH fee and ckERC20 tokens remain locked in the minter indefinitely.

### Citations

**File:** rs/ethereum/cketh/minter/src/main.rs (L430-432)
```rust
    let erc20_tx_fee = estimate_erc20_transaction_fee().await.ok_or_else(|| {
        WithdrawErc20Error::TemporarilyUnavailable("Failed to retrieve current gas fee".to_string())
    })?;
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L448-458)
```rust
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

**File:** rs/ethereum/cketh/minter/src/main.rs (L480-492)
```rust
                    let withdrawal_request = Erc20WithdrawalRequest {
                        max_transaction_fee: erc20_tx_fee,
                        withdrawal_amount: ckerc20_withdrawal_amount,
                        destination,
                        cketh_ledger_burn_index,
                        ckerc20_ledger_id: ckerc20_token.ckerc20_ledger_id,
                        ckerc20_ledger_burn_index,
                        erc20_contract_address: ckerc20_token.erc20_contract_address,
                        from: caller,
                        from_subaccount: from_ckerc20_subaccount
                            .and_then(LedgerSubaccount::from_bytes),
                        created_at: now,
                    };
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L146-177)
```rust
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

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L733-745)
```rust
            WithdrawalRequest::CkErc20(request) => {
                if receipt.status == TransactionStatus::Failure {
                    self.record_reimbursement_request(
                        index,
                        ReimbursementRequest {
                            ledger_burn_index: request.ckerc20_ledger_burn_index,
                            reimbursed_amount: request.withdrawal_amount.change_units(),
                            to: request.from,
                            to_subaccount: request.from_subaccount.clone(),
                            transaction_hash: Some(receipt.transaction_hash),
                        },
                    );
                }
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

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L281-291)
```rust
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
```

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L696-750)
```text
service : (MinterArg) -> {
    // Retrieve the Ethereum address controlled by the minter:
    // * Deposits will be transferred from the helper smart contract to this address
    // * Withdrawals will originate from this address
    // IMPORTANT: Do NOT send ETH to this address directly. Use the helper smart contract instead so that the minter
    // knows to which IC principal the funds should be deposited.
    minter_address : () -> (text);

    // Address of the helper smart contract.
    // Returns "N/A" if the helper smart contract is not set.
    // IMPORTANT:
    // * Use this address to send ETH to the minter to convert it to ckETH.
    // * In case the smart contract needs to be updated the returned address will change!
    //   Always check the address before making a transfer.
    smart_contract_address : () -> (text) query;

    // Estimate the price of a transaction issued by the minter when converting ckETH to ETH.
    eip_1559_transaction_price : (opt Eip1559TransactionPriceArg) -> (Eip1559TransactionPrice) query;

    // Returns internal minter parameters
    get_minter_info : () -> (MinterInfo) query;

    // Withdraw the specified amount in Wei to the given Ethereum address.
    // IMPORTANT: The current gas limit is set to 21,000 for a transaction so withdrawals to smart contract addresses will likely fail.
    withdraw_eth : (WithdrawalArg) -> (variant { Ok : RetrieveEthRequest; Err : WithdrawalError });

    // Withdraw the specified amount of ERC-20 tokens to the given Ethereum address.
    withdraw_erc20 : (WithdrawErc20Arg) -> (variant { Ok : RetrieveErc20Request; Err : WithdrawErc20Error });

    // Retrieve the status of a Eth withdrawal request.
    retrieve_eth_status : (nat64) -> (RetrieveEthStatus);

    // Return details of all withdrawals matching the given search parameter.
    withdrawal_status : (WithdrawalSearchParameter) -> (vec WithdrawalDetail) query;

    // Check if an address is blocked by the minter.
    is_address_blocked : (text) -> (bool) query;

    // Retrieve the status of the minter canister.
    //
    // This is a debug endpoint where backwards-compatibility is not guaranteed.
    get_canister_status : () -> (CanisterStatusResponse);

    // Retrieve events from the minter's audit log.
    // The endpoint can return fewer events than requested to bound the response size.
    // IMPORTANT: this endpoint is meant as a debugging tool and is not guaranteed to be backwards-compatible.
    get_events : (record { start : nat64; length : nat64 }) -> (record { events : vec Event; total_event_count : nat64 }) query;

    // Add a ckERC-20 token to be supported by the minter.
    // This call is restricted to the orchestrator ID.
    add_ckerc20_token : (AddCkErc20Token) -> ();

    // Decode ledger memos produced by the minter when minting (deposits) or burning (withdrawals).
    decode_ledger_memo : (DecodeLedgerMemoArgs) -> (DecodeLedgerMemoResult) query;
}
```

**File:** rs/ethereum/cketh/minter/src/tx.rs (L611-612)
```rust
    const MAX_AGE_NS: u64 = 60_000_000_000_u64; //60 seconds

```
