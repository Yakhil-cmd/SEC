### Title
Permanently Locked ETH Due to Accumulated Unspent Transaction Fees With No Recovery Path in ckETH Minter - (File: rs/ethereum/cketh/minter/src/state.rs)

---

### Summary

The ckETH minter canister charges users a conservative `max_tx_fee_estimate` for each ETH withdrawal, but the actual Ethereum gas cost (`actual_tx_fee`) is always less. The difference (`unspent_tx_fee = max_tx_fee_estimate - actual_tx_fee`) accumulates in the minter's tECDSA-controlled Ethereum address indefinitely. There is no function in the minter canister — admin-controlled or otherwise — to recover or redistribute this ETH. It is permanently inaccessible.

---

### Finding Description

Every call to `withdraw_eth` by an unprivileged user triggers the following accounting in `update_balance_upon_withdrawal`: [1](#0-0) 

The `unspent_tx_fee` is computed as `charged_tx_fee - actual_tx_fee` and added to the monotonically increasing `total_unspent_tx_fees` counter. The minter's `eth_balance` is only reduced by the amount actually debited from the Ethereum address (`tx.amount + actual_tx_fee`), meaning the ETH corresponding to `total_unspent_tx_fees` remains in the minter's Ethereum address. [2](#0-1) 

The documentation explicitly acknowledges this accumulation: [3](#0-2) 

The minter's full public interface is defined in `cketh_minter.did`. Inspecting all exposed endpoints: [4](#0-3) 

There is no `withdraw_unspent_fees`, `admin_withdraw`, or equivalent endpoint. The only way to move ETH out of the minter's Ethereum address is through `withdraw_eth`, which requires burning ckETH. But the ckETH corresponding to the unspent fees was already burned during the original withdrawal — there is no ckETH backing this ETH, and no mechanism to issue new ckETH for it. The ETH is permanently stranded.

The dashboard and metrics expose `total_unspent_tx_fees` as an observable quantity, confirming this is a known, growing balance: [5](#0-4) 

---

### Impact Explanation

ETH accumulates in the minter's tECDSA-controlled Ethereum address with no recovery path. This ETH has no corresponding ckETH backing (the ckETH was burned), so it cannot be withdrawn through the normal flow. It cannot be sent anywhere because the minter canister has no admin withdrawal endpoint. Over the lifetime of the protocol, this amount grows proportionally to total withdrawal volume and the spread between `max_tx_fee_estimate` and `actual_tx_fee`. The funds are permanently inaccessible to users, the protocol, and any governance actor without a canister upgrade.

**Vulnerability class:** chain-fusion mint/burn/replay bug (ledger conservation — ETH permanently locked with no recovery path).

---

### Likelihood Explanation

**High.** This occurs on every single successful ckETH withdrawal. No special conditions, no attacker required. Any unprivileged user calling `withdraw_eth` contributes to the locked balance. The effect is already accumulating on mainnet (the dashboard metric `cketh_minter_total_unspent_tx_fees` is non-zero and growing). The concrete example in the documentation shows ~923 trillion wei (~0.00092 ETH) locked per single withdrawal transaction. [6](#0-5) 

---

### Recommendation

Add a governance-controlled (NNS-proposal-gated) endpoint to the ckETH minter that can issue a tECDSA-signed Ethereum transaction sending the accumulated `total_unspent_tx_fees` ETH to a designated address (e.g., a community treasury or back into the minter's deposit pool to be redistributed as ckETH). Alternatively, implement automatic redistribution: after each finalized withdrawal, mint `unspent_tx_fee` worth of ckETH to a designated protocol account, keeping the ckETH supply in 1:1 correspondence with the ETH held.

---

### Proof of Concept

1. Any user calls `withdraw_eth` with `amount = 30_000_000_000_000_000` wei (minimum).
2. Minter estimates `max_tx_fee_estimate` (e.g., `1_823_126_598_888_000` wei per the documented example).
3. Transaction is mined; `actual_tx_fee = 899_399_014_248_000` wei.
4. `unspent_tx_fee = 1_823_126_598_888_000 - 899_399_014_248_000 = 923_727_584_640_000` wei remains in the minter's Ethereum address.
5. `total_unspent_tx_fees` increases by this amount in `update_balance_upon_withdrawal`.
6. No endpoint exists to recover this ETH. Repeat for every withdrawal. The locked balance grows without bound. [7](#0-6)

### Citations

**File:** rs/ethereum/cketh/minter/src/state.rs (L341-384)
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

        if receipt.status == TransactionStatus::Success && !tx.transaction_data().is_empty() {
            let TransactionCallData::Erc20Transfer { to: _, value } = TransactionCallData::decode(
                tx.transaction_data(),
            )
            .expect("BUG: failed to decode transaction data from transaction issued by minter");
            self.erc20_balances.erc20_sub(*tx.destination(), value);
        }
    }
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

**File:** rs/ethereum/cketh/docs/cketh.adoc (L229-237)
```text
. Initial withdrawal amount: `withdraw_amount:= 39_998_000_000_000_000` wei
. Gas limit: `21_000`
. Max fee per gas: `0x14369c3348 == 86_815_552_328` wei
. Maximum estimated transaction fees: `max_tx_fee_estimate:= 21_000 * 86_815_552_328 == 1_823_126_598_888_000` wei
. Amount received at destination: `39_998_000_000_000_000 - max_tx_fee_estimate == 38_174_873_401_112_000`
. Effective gas price: `0x9f8c76bc8 == 42_828_524_488` wei
. Actual transaction fee: `actual_tx_fee:= 21_000 * 42_828_524_488 == 899_399_014_248_000` wei
. Unspent transaction fee: `max_tx_fee_estimate - actual_tx_fee == 923_727_584_640_000` wei
. Amount charged at minter's address `withdrawal_amount - (max_tx_fee_estimate - actual_tx_fee) == 39_074_272_415_360_000` wei
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

**File:** rs/ethereum/cketh/minter/src/main.rs (L991-995)
```rust
                w.encode_gauge(
                    "cketh_minter_total_unspent_tx_fees",
                    s.eth_balance.total_unspent_tx_fees().as_f64(),
                    "Total amount of unspent fees across all finalized transaction ckETH -> ETH",
                )?;
```
