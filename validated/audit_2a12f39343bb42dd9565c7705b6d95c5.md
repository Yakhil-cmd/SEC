### Title
Accumulated Unspent Transaction Fees Permanently Locked in ckETH Minter with No Claim Mechanism — (`rs/ethereum/cketh/minter/src/state.rs`, `rs/ethereum/cketh/minter/cketh_minter.did`)

---

### Summary

The ckETH minter charges users a conservative `max_tx_fee_estimate` when processing ckETH→ETH withdrawals. The difference between the charged fee and the actual on-chain fee (`total_unspent_tx_fees`) accumulates in the minter's Ethereum address indefinitely. No endpoint exists in the minter's public interface to withdraw, distribute, or reinvest these accumulated fees. The ETH is effectively locked with no programmatic claim mechanism.

---

### Finding Description

When a user calls `withdraw_eth` or `withdraw_erc20`, the minter burns the full `max_tx_fee_estimate` from the user's ckETH balance but only spends `actual_tx_fee` on Ethereum. The remainder (`unspent_tx_fee = max_tx_fee_estimate - actual_tx_fee`) stays in the minter's tECDSA-controlled Ethereum address and is tracked in `EthBalance::total_unspent_tx_fees`. [1](#0-0) 

The accounting update in `update_balance_upon_withdrawal` correctly records this split: [2](#0-1) 

The `total_unspent_tx_fees` counter only ever increases — there is no corresponding decrement path: [3](#0-2) 

Inspecting the full public interface in the Candid file, there is no endpoint to withdraw, distribute, or reinvest these accumulated fees: [4](#0-3) 

The dashboard and metrics expose the growing balance as an observable quantity but provide no action: [5](#0-4) [6](#0-5) 

The same pattern applies to ckERC20 withdrawals, where `req.max_transaction_fee` is charged upfront and any unspent portion accumulates identically: [7](#0-6) 

---

### Impact Explanation

Every ckETH or ckERC20 withdrawal generates a non-zero `unspent_tx_fee` because the minter deliberately overestimates fees to guarantee transaction inclusion even under gas price spikes. Over the lifetime of the protocol, this accumulates to a material ETH balance in the minter's Ethereum address. This ETH:

- Has no corresponding ckETH liability (the ckETH was already burned).
- Cannot be minted back to ckETH (that would break the 1:1 backing invariant).
- Cannot be withdrawn to any Ethereum address (no endpoint exists).
- Cannot be distributed to NNS neurons or ICP holders (no mechanism exists).

The ETH is permanently inaccessible without a governance-approved canister upgrade. This is a direct loss of protocol revenue that should benefit ICP stakeholders.

---

### Likelihood Explanation

This is a certainty, not a probability. Every single successful ckETH or ckERC20 withdrawal produces a non-zero `unspent_tx_fee`. The minter's own documentation confirms this is by design: [8](#0-7) 

The accumulation is continuous and grows monotonically with protocol usage. No code path reduces `total_unspent_tx_fees`.

---

### Recommendation

Implement a governance-gated endpoint (callable only by the NNS governance canister) that:
1. Computes the claimable surplus: `eth_balance - total_cketh_supply_in_wei`.
2. Issues a tECDSA-signed Ethereum transaction to transfer the surplus to a designated treasury address.
3. Records the event in the minter's event log and updates `eth_balance` accordingly.

Alternatively, implement automatic reinvestment by minting ckETH for the accumulated surplus and transferring it to the NNS treasury account on the ckETH ledger.

---

### Proof of Concept

1. User calls `withdraw_eth` with `withdrawal_amount = 30_000_000_000_000_000` wei (0.03 ETH, the minimum).
2. Minter estimates `max_tx_fee_estimate = 1_823_126_598_888_000` wei (example from docs).
3. Minter burns `30_000_000_000_000_000` ckETH from user.
4. Ethereum transaction is mined; `actual_tx_fee = 899_399_014_248_000` wei.
5. `unspent_tx_fee = 1_823_126_598_888_000 - 899_399_014_248_000 = 923_727_584_640_000` wei (~0.00092 ETH) is added to `total_unspent_tx_fees`.
6. Repeat for every withdrawal. After 1,000 withdrawals at similar gas conditions, ~0.92 ETH is locked with no claim path.
7. Querying `get_minter_info` confirms the growing `eth_balance` but no endpoint exists to reclaim the surplus. [9](#0-8) [10](#0-9)

### Citations

**File:** rs/ethereum/cketh/minter/src/state.rs (L355-375)
```rust
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

**File:** rs/ethereum/cketh/minter/src/state.rs (L704-714)
```rust
    fn total_unspent_tx_fees_add(&mut self, value: Wei) {
        self.total_unspent_tx_fees = self
            .total_unspent_tx_fees
            .checked_add(value)
            .unwrap_or_else(|| {
                panic!(
                    "BUG: overflow when adding {} to {}",
                    value, self.total_unspent_tx_fees
                )
            })
    }
```

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L696-775)
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

**File:** rs/ethereum/cketh/minter/templates/dashboard.html (L138-141)
```html
                    <tr id="total-unspent-tx-fees">
                        <th>Total unspent transaction fees (Wei)</th>
                        <td>{{ eth_balance.total_unspent_tx_fees() }}</td>
                    </tr>
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L991-995)
```rust
                w.encode_gauge(
                    "cketh_minter_total_unspent_tx_fees",
                    s.eth_balance.total_unspent_tx_fees().as_f64(),
                    "Total amount of unspent fees across all finalized transaction ckETH -> ETH",
                )?;
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

**File:** rs/ethereum/cketh/minter/src/state/tests.rs (L1407-1423)
```rust
        assert_eq!(
            eth_balance_after_successful_withdrawal,
            EthBalance {
                eth_balance: eth_balance_before_withdrawal
                    .eth_balance
                    .checked_sub(Wei::from(9_934_054_275_043_000_u64))
                    .unwrap(),
                total_effective_tx_fees: eth_balance_before_withdrawal
                    .total_effective_tx_fees
                    .checked_add(Wei::from(98_449_949_997_000_u64))
                    .unwrap(),
                total_unspent_tx_fees: eth_balance_before_withdrawal
                    .total_unspent_tx_fees
                    .checked_add(Wei::from(65_945_724_957_000_u64))
                    .unwrap(),
            }
        );
```
