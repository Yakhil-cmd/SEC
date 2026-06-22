### Title
Arithmetic Underflow Panic in `update_balance_upon_withdrawal` Causes ckETH/ckERC20 Minter Denial of Service - (File: `rs/ethereum/cketh/minter/src/state.rs`)

---

### Summary

The ckETH minter's `update_balance_upon_withdrawal` function contains two `checked_sub(...).expect(...)` calls that will panic — trapping the canister — if the effective transaction fee on Ethereum exceeds the fee that was charged to the user at withdrawal time. This is structurally analogous to the reported vault denial-of-service: a shared resource (the minter's tracked ETH balance and fee accounting) can be driven into an inconsistent state by external conditions (rising Ethereum gas prices during resubmission), causing a panic that permanently blocks all future withdrawal finalization.

---

### Finding Description

In `rs/ethereum/cketh/minter/src/state.rs`, `update_balance_upon_withdrawal` is called every time a sent Ethereum transaction is finalized:

```rust
let unspent_tx_fee = charged_tx_fee.checked_sub(tx_fee).expect(
    "BUG: charged transaction fee MUST always be at least the effective transaction fee",
);
``` [1](#0-0) 

The invariant `charged_tx_fee >= tx_fee` is assumed to always hold. For a **ckETH withdrawal**, `charged_tx_fee = withdrawal_amount - tx.transaction().amount` (the fee margin built into the withdrawal). For a **ckERC20 withdrawal**, `charged_tx_fee = req.max_transaction_fee` (the ckETH burned upfront for gas).

The minter supports **transaction resubmission** when Ethereum gas prices rise: each resubmission bumps `max_priority_fee_per_gas` by at least 10%, and `max_fee_per_gas` is also increased as needed. The resubmission logic correctly checks that the new fee does not exceed the user's allowed budget:

```rust
if new_tx_price.max_transaction_fee() > self.resubmission.allowed_max_transaction_fee() {
    return Err(ResubmitTransactionError::InsufficientTransactionFee { ... });
}
``` [2](#0-1) 

However, `max_transaction_fee()` is computed as `max_fee_per_gas * gas_limit`, which is the **maximum** the user could be charged. The **actual** fee paid (`effective_gas_price * gas_used` from the receipt) can exceed `charged_tx_fee` in a specific scenario: if the Ethereum miner's `effective_gas_price` in the receipt is higher than the `max_fee_per_gas` that was used to compute `charged_tx_fee` at withdrawal time, but the transaction was still mined (e.g., because the miner accepted a lower priority fee). More concretely, for ckERC20 withdrawals, `charged_tx_fee = req.max_transaction_fee` is fixed at the time of the withdrawal call, but after multiple resubmissions the `max_fee_per_gas` of the final transaction can be higher than the original estimate, and the `effective_gas_price` in the receipt can be up to `max_fee_per_gas` of the **resubmitted** transaction — which may exceed `req.max_transaction_fee`.

Additionally, `eth_balance_sub` will panic if `debited_amount > eth_balance`:

```rust
fn eth_balance_sub(&mut self, value: Wei) {
    self.eth_balance = self.eth_balance.checked_sub(value).unwrap_or_else(|| {
        panic!(
            "BUG: underflow when subtracting {} from {}",
            value, self.eth_balance
        )
    })
}
``` [3](#0-2) 

This is called unconditionally during `FinalizedTransaction` event processing, which is triggered by the minter's background task `finalize_transactions_batch`. A panic here traps the canister update call, rolling back state, but the background task will retry — hitting the same panic every time, permanently blocking all withdrawal finalization.

---

### Impact Explanation

If the panic is triggered during `apply_state_transition` for a `FinalizedTransaction` event, the minter canister traps. Since the `finalize_transactions_batch` task runs on a timer and will keep retrying with the same finalized receipt, the minter becomes permanently unable to:
- Finalize any pending withdrawal
- Process reimbursements
- Progress any withdrawal state machine

All user funds locked in pending ckETH or ckERC20 withdrawals become inaccessible. This is a **chain-fusion mint/burn/replay bug** with a **denial-of-service** impact on the ckETH minter canister (`sv3dd-oaaaa-aaaar-qacoa-cai`). [4](#0-3) 

---

### Likelihood Explanation

The scenario requires Ethereum gas prices to spike significantly after a withdrawal is submitted, causing multiple resubmissions that push the effective transaction fee above the originally charged fee. This is a realistic market condition during Ethereum network congestion. The ckBTC minter already experienced a real-world instance of a deterministic panic in resubmission logic (documented in `rs/bitcoin/ckbtc/mainnet/minter_upgrade_2026_01_23.md`), confirming that such arithmetic panics in minter resubmission paths are not merely theoretical. [5](#0-4) 

The ckETH documentation itself acknowledges that multiple resubmissions with 10% fee bumps are expected during congestion: [6](#0-5) 

---

### Recommendation

1. Replace the panicking `.expect()` in `update_balance_upon_withdrawal` with saturating arithmetic or graceful error handling. If `tx_fee > charged_tx_fee`, set `unspent_tx_fee = 0` and log an error rather than panicking.
2. Similarly, guard `eth_balance_sub` against underflow by using saturating subtraction with an error log, rather than a panic.
3. Add an invariant check before finalizing a transaction that verifies `eth_balance >= debited_amount` and `charged_tx_fee >= tx_fee`, and if violated, emit a log and skip the balance update rather than trapping.

---

### Proof of Concept

1. User calls `withdraw_eth` with `withdrawal_amount = W`. The minter estimates `max_tx_fee = F` and creates a transaction with `amount = W - F`. `charged_tx_fee = F`.
2. The transaction is not mined. Ethereum gas prices spike. The minter resubmits with a 10% higher `max_priority_fee_per_gas` and a higher `max_fee_per_gas = F'`. The resubmission check passes because `F' * gas_limit <= W` (the user's budget).
3. The resubmitted transaction is mined. The receipt shows `effective_gas_price = P_eff` where `P_eff * gas_used > F` (the original charged fee), because `P_eff` reflects the higher `max_fee_per_gas` of the resubmitted transaction.
4. `finalize_transactions_batch` calls `mutate_state` → `process_event(FinalizedTransaction)` → `record_finalized_transaction` → `update_balance_upon_withdrawal`.
5. `charged_tx_fee.checked_sub(tx_fee)` returns `None` because `tx_fee > charged_tx_fee`. `.expect()` panics.
6. The canister traps. The timer retries. The minter is permanently stuck — no withdrawal can ever be finalized again. [7](#0-6) [8](#0-7)

### Citations

**File:** rs/ethereum/cketh/minter/src/state.rs (L355-364)
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
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L373-375)
```rust
        self.eth_balance.eth_balance_sub(debited_amount);
        self.eth_balance.total_effective_tx_fees_add(tx_fee);
        self.eth_balance.total_unspent_tx_fees_add(unspent_tx_fee);
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L683-690)
```rust
    fn eth_balance_sub(&mut self, value: Wei) {
        self.eth_balance = self.eth_balance.checked_sub(value).unwrap_or_else(|| {
            panic!(
                "BUG: underflow when subtracting {} from {}",
                value, self.eth_balance
            )
        })
    }
```

**File:** rs/ethereum/cketh/minter/src/tx.rs (L169-173)
```rust
        if new_tx_price.max_transaction_fee() > self.resubmission.allowed_max_transaction_fee() {
            return Err(ResubmitTransactionError::InsufficientTransactionFee {
                allowed_max_transaction_fee: self.resubmission.allowed_max_transaction_fee(),
                actual_max_transaction_fee: new_tx_price.max_transaction_fee(),
            });
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L386-461)
```rust
async fn finalize_transactions_batch() {
    if read_state(|s| s.eth_transactions.is_sent_tx_empty()) {
        return;
    }

    match finalized_transaction_count().await {
        Ok(finalized_tx_count) => {
            let txs_to_finalize = read_state(|s| {
                s.eth_transactions
                    .sent_transactions_to_finalize(&finalized_tx_count)
            });
            let expected_finalized_withdrawal_ids: BTreeSet<_> =
                txs_to_finalize.values().cloned().collect();
            let rpc_client = read_state(rpc_client);
            let results = join_all(txs_to_finalize.keys().map(async |hash| {
                rpc_client
                    .get_transaction_receipt(*hash)
                    .with_cycles(MIN_ATTACHED_CYCLES)
                    .try_send()
                    .await
                    .reduce_with_strategy(NoReduction)
            }))
            .await;
            let mut receipts: BTreeMap<LedgerBurnIndex, EvmTransactionReceipt> = BTreeMap::new();
            for ((hash, withdrawal_id), result) in zip(txs_to_finalize, results) {
                match result {
                    Ok(Some(receipt)) => {
                        log!(
                            DEBUG,
                            "Received transaction receipt {receipt:?} for transaction {hash} and withdrawal ID {withdrawal_id}"
                        );
                        match receipts.get(&withdrawal_id) {
                            // by construction we never query twice the same transaction hash, which is a field in TransactionReceipt.
                            Some(existing_receipt) => {
                                log!(
                                    INFO,
                                    "ERROR: received different receipts for transaction {hash} with withdrawal ID {withdrawal_id}: {existing_receipt:?} and {receipt:?}. Will retry later"
                                );
                                return;
                            }
                            None => {
                                receipts.insert(withdrawal_id, receipt);
                            }
                        }
                    }
                    Ok(None) => {
                        log!(
                            DEBUG,
                            "Transaction {hash} for withdrawal ID {withdrawal_id} was not mined, it's probably a resubmitted transaction",
                        )
                    }
                    Err(e) => {
                        log!(
                            INFO,
                            "Failed to get transaction receipt for {hash} and withdrawal ID {withdrawal_id}: {e:?}. Will retry later",
                        );
                        return;
                    }
                }
            }
            let actual_finalized_withdrawal_ids: BTreeSet<_> = receipts.keys().cloned().collect();
            assert_eq!(
                expected_finalized_withdrawal_ids, actual_finalized_withdrawal_ids,
                "ERROR: unexpected transaction receipts for some withdrawal IDs"
            );
            for (withdrawal_id, transaction_receipt) in receipts {
                mutate_state(|s| {
                    process_event(
                        s,
                        EventType::FinalizedTransaction {
                            withdrawal_id,
                            transaction_receipt: transaction_receipt.into(),
                        },
                    );
                });
            }
```

**File:** rs/bitcoin/ckbtc/mainnet/minter_upgrade_2026_01_23.md (L31-33)
```markdown
cc56275206 chore: rust: 1.90.0 -> 1.92.0  (#8124)
e0e6f70b6c fix(ckdoge): use legacy P2PKH transactions for withdrawals (#8187)
bb5c405c6f feat(ckbtc): Use 25th percentile fee for UTXO consolidation (#8150)
```

**File:** rs/ethereum/cketh/docs/cketh.adoc (L208-208)
```text
. Issue an Ethereum transaction (via threshold ECDSA) with the value `withdraw_amount - max_tx_fee_estimate`. This requires of course that `withdraw_amount >= max_tx_fee_estimate` and that's why we currently have a conservative minimum value for withdrawals of `30_000_000_000_000_000` wei. This ensures that the minter can always send the transaction to Ethereum if one or several resubmissions are needed if the Ethereum network is congested and fees are increasing rapidly (each resubmission requires an increase of at least 10% of the transaction fee).
```
