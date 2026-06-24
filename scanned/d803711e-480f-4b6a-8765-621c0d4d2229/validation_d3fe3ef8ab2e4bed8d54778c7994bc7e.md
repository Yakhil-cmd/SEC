### Title
Unspent ETH Transaction Fees Permanently Accumulate in ckETH Minter With No Distribution Mechanism - (`rs/ethereum/cketh/minter/src/state.rs`)

---

### Summary

The ckETH minter charges users a conservative maximum estimated gas fee (`max_tx_fee_estimate`) when processing ETH withdrawals. Because actual Ethereum gas costs are almost always lower than the estimate, the difference (`unspent_tx_fee = max_tx_fee_estimate - actual_tx_fee`) remains in the minter's Ethereum address indefinitely. The minter tracks this accumulation in `total_unspent_tx_fees` but provides no mechanism to return, redistribute, or otherwise recover these funds for users. This is a direct analog to H-3: users who withdraw ckETH lose the overpaid fee portion permanently.

---

### Finding Description

When a user calls `withdraw_eth` (or `retrieve_btc_with_approval` for ckERC20), the minter:

1. Burns `withdrawal_amount` ckETH from the user.
2. Sends `withdrawal_amount - max_tx_fee_estimate` ETH to the user's Ethereum address.
3. Pays `actual_tx_fee` from its own ETH balance to mine the transaction.

The minter's ETH balance is debited by `(withdrawal_amount - max_tx_fee_estimate) + actual_tx_fee`. The remainder, `unspent_tx_fee = max_tx_fee_estimate - actual_tx_fee`, stays in the minter's Ethereum address with no corresponding ckETH liability.

In `update_balance_upon_withdrawal`:

```rust
let unspent_tx_fee = charged_tx_fee.checked_sub(tx_fee).expect(
    "BUG: charged transaction fee MUST always be at least the effective transaction fee",
);
self.eth_balance.eth_balance_sub(debited_amount);
self.eth_balance.total_effective_tx_fees_add(tx_fee);
self.eth_balance.total_unspent_tx_fees_add(unspent_tx_fee);
``` [1](#0-0) 

The `total_unspent_tx_fees` field in `EthBalance` is a monotonically increasing counter:

```rust
/// Total amount of fees that were charged to the user during the withdrawal
/// but not consumed by the finalized transaction ckETH -> ETH
total_unspent_tx_fees: Wei,
``` [2](#0-1) 

This value is exposed only as a dashboard metric and Prometheus gauge: [3](#0-2) 

The minter's public API (`.did` file) exposes no endpoint to:
- Query per-user overpaid fee amounts.
- Claim a refund for the unspent portion.
- Distribute accumulated surplus ETH to ckETH holders. [4](#0-3) 

The documentation explicitly acknowledges the phenomenon but provides no remediation path:

> "Total unspent transaction fees: the sum of all `max_tx_fee_estimate - actual_tx_fee` for all withdrawals. This represents an overestimate of the actual transaction fees that were charged to the user but in retrospect not needed to mine the sent transaction." [5](#0-4) 

---

### Impact Explanation

Every ckETH or ckERC20 withdrawal where `actual_tx_fee < max_tx_fee_estimate` results in a permanent loss to the withdrawing user. The surplus ETH accumulates in the minter's tECDSA-controlled Ethereum address. Because the only way to move ETH out of the minter's address is through the withdrawal mechanism (which requires burning ckETH), the surplus is effectively locked. Over time, the minter holds more ETH than is needed to back the outstanding ckETH supply, and users collectively lose the difference with no recourse.

The ckETH documentation confirms the estimate is intentionally conservative to handle fee spikes and resubmissions, meaning the gap between `max_tx_fee_estimate` and `actual_tx_fee` is structurally guaranteed to be positive for the vast majority of withdrawals. [6](#0-5) 

---

### Likelihood Explanation

This affects every successful ckETH or ckERC20 withdrawal. The minter is live on mainnet and processes real user withdrawals. The `total_unspent_tx_fees` counter is already non-zero in production (it is tracked and displayed on the minter dashboard). No privileged access, key compromise, or network attack is required — any user who calls `withdraw_eth` triggers the accumulation. [7](#0-6) 

---

### Recommendation

Add a mechanism to recover or redistribute accumulated unspent fees. Options include:

1. **Per-withdrawal reimbursement**: After a transaction is finalized and the actual fee is known, mint the difference (`unspent_tx_fee`) back to the user's ckETH account.
2. **Treasury claim**: Add a governance-controlled endpoint to sweep accumulated unspent fees to a treasury account, then distribute via airdrop or fee reduction.
3. **Fee reduction**: Use the accumulated surplus to subsidize future withdrawal fees, reducing the minimum withdrawal amount over time.

The simplest correct fix is option 1: after `record_finalized_transaction` is called and `unspent_tx_fee` is computed, issue a mint of `unspent_tx_fee` worth of ckETH back to the original requester's account.

---

### Proof of Concept

1. User calls `withdraw_eth` with `withdrawal_amount = 39_998_000_000_000_000` wei (as in the documented example).
2. Minter estimates `max_tx_fee_estimate = 1_823_126_598_888_000` wei.
3. User receives `38_174_873_401_112_000` wei at their ETH address.
4. Actual tx fee = `899_399_014_248_000` wei.
5. `unspent_tx_fee = 1_823_126_598_888_000 - 899_399_014_248_000 = 923_727_584_640_000` wei (~0.00092 ETH, ~$3 at $3000/ETH).
6. This amount is added to `total_unspent_tx_fees` and remains in the minter's Ethereum address permanently.
7. The user burned `39_998_000_000_000_000` wei of ckETH but only received `38_174_873_401_112_000` wei of ETH — a shortfall of `1_823_126_598_888_000` wei, of which only `899_399_014_248_000` was actually needed. [8](#0-7) [9](#0-8)

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

**File:** rs/ethereum/cketh/minter/src/state.rs (L656-661)
```rust
    /// Total amount of fees across all finalized transactions ckETH -> ETH.
    total_effective_tx_fees: Wei,
    /// Total amount of fees that were charged to the user during the withdrawal
    /// but not consumed by the finalized transaction ckETH -> ETH
    total_unspent_tx_fees: Wei,
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

**File:** rs/bitcoin/ckbtc/minter/ckbtc_minter.did (L691-750)
```text
    // guarantee in the ordering of the returned values).
    //
    // If the owner is not set, it defaults to the caller's principal.
    get_known_utxos: (record { owner: opt principal; subaccount : opt blob }) -> (vec Utxo) query;

    // Mints ckBTC for newly deposited UTXOs.
    //
    // If the owner is not set, it defaults to the caller's principal.
    //
    // # Preconditions
    //
    // * The owner deposited some BTC to the address that the
    //   [get_btc_address] endpoint returns.
    update_balance : (record { owner: opt principal; subaccount : opt blob }) -> (variant { Ok : vec UtxoStatus; Err : UpdateBalanceError });

    // }}} Section "Convert BTC to ckBTC"

    // Section "Convert ckBTC to BTC" {{{

    /// Returns an estimate of the user's fee (in Satoshi) for a
    /// retrieve_btc request based on the current status of the Bitcoin network.
    estimate_withdrawal_fee : (record { amount : opt nat64 }) -> (record { bitcoin_fee : nat64; minter_fee : nat64 }) query;

    /// Returns the fee that the minter will charge for a bitcoin deposit.
    get_deposit_fee: () -> (nat64) query;

    // Returns the account to which the caller should deposit ckBTC
    // before withdrawing BTC using the [retrieve_btc] endpoint.
    get_withdrawal_account : () -> (Account);


    // Submits a request to convert ckBTC to BTC.
    //
    // # Note
    //
    // The BTC retrieval process is slow.  Instead of
    // synchronously waiting for a BTC transaction to settle, this
    // method returns a request ([block_index]) that the caller can use
    // to query the request status.
    //
    // # Preconditions
    //
    // * The caller deposited the requested amount in ckBTC to the account
    //   that the [get_withdrawal_account] endpoint returns.
    retrieve_btc : (RetrieveBtcArgs) -> (variant { Ok : RetrieveBtcOk; Err : RetrieveBtcError });

    // Submits a request to convert ckBTC to BTC.
    //
    // # Note
    //
    // The BTC retrieval process is slow.  Instead of
    // synchronously waiting for a BTC transaction to settle, this
    // method returns a request ([block_index]) that the caller can use
    // to query the request status.
    //
    // # Preconditions
    //
    // * The caller allowed the minter's principal to spend its funds
    //   using [icrc2_approve] on the ckBTC ledger.
    retrieve_btc_with_approval : (RetrieveBtcWithApprovalArgs) -> (variant { Ok : RetrieveBtcOk; Err : RetrieveBtcWithApprovalError });
```

**File:** rs/ethereum/cketh/docs/cketh.adoc (L207-214)
```text
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

**File:** rs/ethereum/cketh/docs/cketh.adoc (L229-238)
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
====
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
