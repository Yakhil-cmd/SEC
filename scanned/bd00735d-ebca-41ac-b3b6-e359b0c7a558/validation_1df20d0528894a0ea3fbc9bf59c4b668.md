### Title
ckETH Minter Has No Mechanism to Recover Accumulated Unspent Transaction Fees Locked at Its Ethereum Address — (`rs/ethereum/cketh/minter/src/state.rs`, `rs/ethereum/cketh/minter/src/main.rs`)

---

### Summary

The ckETH minter canister controls an Ethereum address via threshold ECDSA. Every ckETH→ETH withdrawal charges the user a conservative `max_tx_fee_estimate` but only spends the lower `actual_tx_fee`. The difference (`unspent_tx_fee = max_tx_fee_estimate − actual_tx_fee`) remains at the minter's Ethereum address and is tracked in `EthBalance::total_unspent_tx_fees`, but there is no canister endpoint — admin or otherwise — that can issue an Ethereum transaction to transfer this accumulated ETH out. The funds are permanently locked until a canister upgrade adds such a function.

---

### Finding Description

The `EthBalance` struct in `rs/ethereum/cketh/minter/src/state.rs` tracks three fields:

```rust
pub struct EthBalance {
    eth_balance: Wei,
    total_effective_tx_fees: Wei,
    total_unspent_tx_fees: Wei,   // ← accumulates with every withdrawal
}
``` [1](#0-0) 

After each finalized withdrawal, `update_balance_upon_withdrawal` computes:

```rust
let unspent_tx_fee = charged_tx_fee.checked_sub(tx_fee)…;
self.eth_balance.eth_balance_sub(debited_amount);
self.eth_balance.total_unspent_tx_fees_add(unspent_tx_fee);
``` [2](#0-1) 

The user burned `withdrawal_amount` ckETH, but the minter's Ethereum address was only debited `withdrawal_amount − unspent_tx_fee`. The surplus `unspent_tx_fee` stays at the minter's Ethereum address, unbacked by any ckETH. The minter's own documentation acknowledges this:

> "Note that invalid deposits are not accounted for and so this value might be less than what is displayed by Etherscan… Also, some transactions may have gone directly to the minter's address without going via the helper smart contract." [3](#0-2) 

The complete public interface of the minter canister (from `cketh_minter.did`) exposes only:

- `withdraw_eth` — requires burning ckETH; cannot be used to recover unbacked ETH
- `withdraw_erc20` — same constraint
- Query-only endpoints [4](#0-3) 

There is no `admin_transfer_eth`, `recover_unspent_fees`, or equivalent endpoint. The `main.rs` timer loop only calls `scrape_logs`, `process_retrieve_eth_requests`, and `process_reimbursement` — none of which drain the unspent-fee surplus. [5](#0-4) 

---

### Impact Explanation

Every ckETH→ETH withdrawal permanently locks `max_tx_fee_estimate − actual_tx_fee` wei at the minter's Ethereum address. The minter's own dashboard exposes `total_unspent_tx_fees` as a growing counter with no corresponding drain path. This ETH is not backed by any ckETH (the ckETH was already burned), so it cannot be recovered via the normal `withdraw_eth` flow. The funds are inaccessible until a governance-approved canister upgrade adds a recovery function. This is a **chain-fusion ledger conservation bug**: real ETH value is permanently removed from circulation without any corresponding ckETH redemption path.

---

### Likelihood Explanation

The condition is triggered by every single ckETH→ETH withdrawal. The minter deliberately over-estimates fees to guarantee transaction inclusion, so `unspent_tx_fee > 0` is the normal case, not an edge case. The documentation example shows ~0.00092 ETH unspent per withdrawal. With thousands of withdrawals on mainnet, the total locked amount is non-trivial and grows monotonically. No special attacker action is required; ordinary user withdrawals are sufficient.

---

### Recommendation

Add a governance-restricted endpoint (callable only by the NNS root/controller) that issues a signed Ethereum transaction transferring the accumulated unspent-fee balance to a designated address:

```rust
#[update]
async fn admin_recover_eth(recipient: String, amount: Wei) {
    // restrict to controller
    assert_eq!(ic_cdk::api::msg_caller(), read_state(|s| s.controller));
    // build and sign an Ethereum transfer via tECDSA, similar to process_retrieve_eth_requests
}
```

This mirrors the pattern already used for `withdraw_eth` but without requiring a ckETH burn, since the recovered ETH is unbacked surplus.

---

### Proof of Concept

1. User calls `withdraw_eth` with `withdrawal_amount = 39_998_000_000_000_000` wei.
2. Minter burns `39_998_000_000_000_000` wei ckETH from user's ledger account.
3. Minter sends `38_174_873_401_112_000` wei to destination (amount minus `max_tx_fee_estimate`).
4. Ethereum charges actual fee `899_399_014_248_000` wei.
5. `unspent_tx_fee = 1_823_126_598_888_000 − 899_399_014_248_000 = 923_727_584_640_000` wei remains at minter's Ethereum address.
6. `total_unspent_tx_fees` increases by `923_727_584_640_000` wei.
7. `eth_balance` decreases by only `debited_amount = 39_074_272_415_360_000` wei (not the full `withdrawal_amount`).
8. The surplus `923_727_584_640_000` wei is at the minter's Ethereum address, unbacked by ckETH, with no callable endpoint to retrieve it. [6](#0-5) [7](#0-6)

### Citations

**File:** rs/ethereum/cketh/minter/src/state.rs (L341-375)
```rust
    fn update_balance_upon_withdrawal(
        &mut self,
        withdrawal_id: &LedgerBurnIndex,
        receipt: &TransactionReceipt,
    ) {
        let tx_fee = receipt.effective_transaction_fee();
        let tx = self
            .eth_transactions
            .get_finalized_transaction(withdrawal_id)
            .expect("BUG: missing finalized transaction");
        let withdrawal_request = self
            .eth_transactions
            .get_processed_withdrawal_request(withdrawal_id)
            .expect("BUG: missing withdrawal request");
        let charged_tx_fee = match withdrawal_request {
            WithdrawalRequest::CkEth(req) => req
                .withdrawal_amount
                .checked_sub(tx.transaction().amount)
                .expect("BUG: withdrawal amount MUST always be at least the transaction amount"),
            WithdrawalRequest::CkErc20(req) => req.max_transaction_fee,
        };
        let unspent_tx_fee = charged_tx_fee.checked_sub(tx_fee).expect(
            "BUG: charged transaction fee MUST always be at least the effective transaction fee",
        );
        let debited_amount = match receipt.status {
            TransactionStatus::Success => tx
                .transaction()
                .amount
                .checked_add(tx_fee)
                .expect("BUG: debited amount always fits into U256"),
            TransactionStatus::Failure => tx_fee,
        };
        self.eth_balance.eth_balance_sub(debited_amount);
        self.eth_balance.total_effective_tx_fees_add(tx_fee);
        self.eth_balance.total_unspent_tx_fees_add(unspent_tx_fee);
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L647-661)
```rust
#[derive(Clone, Eq, PartialEq, Debug)]
pub struct EthBalance {
    /// Amount of ETH controlled by the minter's address via tECDSA.
    /// Note that invalid deposits are not accounted for and so this value
    /// might be less than what is displayed by Etherscan
    /// or retrieved by the JSON-RPC call `eth_getBalance`.
    /// Also, some transactions may have gone directly to the minter's address
    /// without going via the helper smart contract.
    eth_balance: Wei,
    /// Total amount of fees across all finalized transactions ckETH -> ETH.
    total_effective_tx_fees: Wei,
    /// Total amount of fees that were charged to the user during the withdrawal
    /// but not consumed by the finalized transaction ckETH -> ETH
    total_unspent_tx_fees: Wei,
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

**File:** rs/ethereum/cketh/minter/src/main.rs (L75-93)
```rust
fn setup_timers() {
    ic_cdk_timers::set_timer(Duration::from_secs(0), async {
        // Initialize the minter's public key to make the address known.
        let _ = lazy_call_ecdsa_public_key().await;
    });
    // Start scraping logs immediately after the install, then repeat with the interval.
    ic_cdk_timers::set_timer(Duration::from_secs(0), async {
        scrape_logs().await;
    });
    ic_cdk_timers::set_timer_interval(SCRAPING_ETH_LOGS_INTERVAL, async || {
        scrape_logs().await;
    });
    ic_cdk_timers::set_timer_interval(PROCESS_ETH_RETRIEVE_TRANSACTIONS_INTERVAL, async || {
        process_retrieve_eth_requests().await;
    });
    ic_cdk_timers::set_timer_interval(PROCESS_REIMBURSEMENT, async || {
        process_reimbursement().await;
    });
}
```
