### Title
Missing Expiry Parameter in `withdraw_eth` Allows Stale Withdrawal Execution at Unfavorable Gas Prices — (File: rs/ethereum/cketh/minter/src/main.rs)

---

### Summary

The `withdraw_eth` function in the ckETH minter canister accepts withdrawal requests without any expiry or deadline parameter. Once submitted, the user's ckETH is immediately and irrevocably burned, and the withdrawal request is enqueued for asynchronous processing. The Ethereum transaction fee is estimated at transaction-creation time (when the minter dequeues the request), not at submission time. If the minter's queue is congested or the minter is temporarily unavailable, the request may be processed much later at significantly higher Ethereum gas prices, causing the user to receive substantially less ETH than expected with no ability to cancel.

---

### Finding Description

`WithdrawalArg` in `rs/ethereum/cketh/minter/src/endpoints.rs` accepts only `amount`, `recipient`, and `from_subaccount` — no expiry or deadline field:

```rust
pub struct WithdrawalArg {
    pub amount: Nat,
    pub recipient: String,
    pub from_subaccount: Option<Subaccount>,
}
``` [1](#0-0) 

The `withdraw_eth` handler in `rs/ethereum/cketh/minter/src/main.rs` immediately burns the caller's ckETH and enqueues an `EthWithdrawalRequest` with no expiry check:

```rust
let withdrawal_request = EthWithdrawalRequest {
    withdrawal_amount: amount,
    destination,
    ledger_burn_index,
    from: caller,
    from_subaccount: from_subaccount.and_then(LedgerSubaccount::from_bytes),
    created_at: Some(now),   // ← only records creation time; no expiry
};
``` [2](#0-1) 

The request is placed into `pending_withdrawal_requests: VecDeque<WithdrawalRequest>` and processed in FIFO order: [3](#0-2) 

The `EthWithdrawalRequest` struct itself has no expiry field — only `created_at: Option<u64>`: [4](#0-3) 

The Ethereum transaction fee is estimated at transaction-creation time (when the minter dequeues the request), not at withdrawal-submission time. The ckETH documentation confirms: *"The exact fee deducted depends on the dynamic Ethereum transaction fees used at the time the transaction was created."* [5](#0-4) 

There is no `cancel_withdrawal` or equivalent endpoint in the minter's public interface: [6](#0-5) 

The same pattern applies to `withdraw_erc20` (`WithdrawErc20Arg` also has no expiry field) and to `retrieve_btc` (`RetrieveBtcArgs` has only `amount` and `address`): [7](#0-6) [8](#0-7) 

---

### Impact Explanation

Once `withdraw_eth` is called, the user's ckETH is burned immediately and irreversibly. The Ethereum transaction fee is deducted from the withdrawal amount at the time the minter creates the on-chain transaction. If Ethereum gas prices spike significantly while the request sits in the queue (e.g., during network congestion events where gas prices increase 10–100×), the user receives substantially less ETH than they expected when submitting the request. Because there is no expiry parameter and no cancellation endpoint, the user has no recourse. The financial loss is bounded by the gas fee difference but can be material: at 21,000 gas limit, a spike from 10 gwei to 500 gwei costs an additional ~0.01 ETH (~$25 at $2,500/ETH) per withdrawal.

---

### Likelihood Explanation

Ethereum gas prices are well-documented to be highly volatile. Any unprivileged user can call `withdraw_eth` — no special role is required. The minter processes requests in FIFO order, and during periods of high demand or temporary minter unavailability, requests can be delayed. The scenario (submit during low-gas conditions, processed during high-gas conditions) is realistic and has occurred repeatedly on Ethereum mainnet.

---

### Recommendation

Add an optional `expires_at: Option<u64>` field (nanoseconds since Unix epoch) to `WithdrawalArg` and `WithdrawErc20Arg`. When the minter dequeues a request for transaction creation, it should check whether `ic_cdk::api::time() > expires_at`. If so, the request should be cancelled and the burned ckETH/ckERC20 reimbursed to the user, analogous to the existing reimbursement flow for failed transactions.

---

### Proof of Concept

1. User calls `withdraw_eth` with `amount = 1 ETH` when Ethereum gas is 10 gwei. User expects to receive approximately `1 ETH − 21,000 × 10 gwei = 0.99979 ETH`.
2. The minter's `pending_withdrawal_requests` queue contains many prior requests (up to the system limit).
3. While the request is queued, Ethereum network congestion causes gas to spike to 500 gwei.
4. When the minter finally dequeues and creates the transaction, it estimates `max_tx_fee = 21,000 × 500 gwei = 0.0105 ETH`.
5. The user receives `1 ETH − 0.0105 ETH = 0.9895 ETH` — a loss of ~0.0103 ETH compared to expectations.
6. The user's ckETH was burned at step 1 and cannot be recovered; no cancellation endpoint exists in the minter's public API.

### Citations

**File:** rs/ethereum/cketh/minter/src/endpoints.rs (L208-213)
```rust
#[derive(CandidType, Deserialize)]
pub struct WithdrawalArg {
    pub amount: Nat,
    pub recipient: String,
    pub from_subaccount: Option<Subaccount>,
}
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L314-322)
```rust
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

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L362-363)
```rust
    pub(in crate::state) pending_withdrawal_requests: VecDeque<WithdrawalRequest>,
    // Processed withdrawal requests (transaction created, sent, or finalized).
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

**File:** rs/ethereum/cketh/minter/src/endpoints/ckerc20.rs (L6-13)
```rust
#[derive(CandidType, Deserialize)]
pub struct WithdrawErc20Arg {
    pub amount: Nat,
    pub ckerc20_ledger_id: Principal,
    pub recipient: String,
    pub from_cketh_subaccount: Option<Subaccount>,
    pub from_ckerc20_subaccount: Option<Subaccount>,
}
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L25-32)
```rust
#[derive(Clone, Eq, PartialEq, Debug, CandidType, Deserialize)]
pub struct RetrieveBtcArgs {
    // amount to retrieve in satoshi
    pub amount: u64,

    // address where to send bitcoins
    pub address: String,
}
```
