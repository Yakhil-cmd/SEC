Now I have all the information needed for a complete analysis. Let me trace the exact code path.

**Key facts established:**

1. `transaction_amount()` in `tx.rs` line 313-315 returns `self.transaction.transaction().amount` — the ETH `amount` field of the EIP-1559 transaction, which equals `withdrawal_amount - max_fee_estimate`.

2. `record_finalized_transaction` in `mod.rs` line 727 sets `reimbursed_amount: finalized_tx.transaction_amount().change_units()` for ckETH failures — i.e., `withdrawal_amount - max_fee_estimate`.

3. The correct value should be `withdrawal_amount - effective_fee` where `effective_fee = receipt.effective_gas_price * receipt.gas_used`.

4. The test helper `transaction_receipt` at `tests.rs` line 2906 sets `effective_gas_price: signed_tx.transaction().max_fee_per_gas` — always equal to `max_fee_per_gas`, so the test only exercises the degenerate case where `effective_fee == max_fee_estimate` and the two formulas coincide.

---

### Title
ckETH Withdrawal Failure Reimburses `transaction_amount` Instead of `withdrawal_amount - effective_fee`, Causing Systematic User Underpayment — (`rs/ethereum/cketh/minter/src/state/transactions/mod.rs`)

### Summary
When a ckETH withdrawal transaction fails on Ethereum, `record_finalized_transaction` reimburses the user `finalized_tx.transaction_amount()` (= `withdrawal_amount - max_fee_estimate`) instead of `withdrawal_amount - effective_fee`. The difference `max_fee_estimate - effective_fee` is silently retained by the minter as unaccounted ETH, causing a systematic financial loss to any user whose withdrawal fails when `effective_gas_price < max_fee_per_gas`.

### Finding Description

**Transaction creation** (`create_transaction`, `mod.rs` lines 1122–1145):

For `CkEth`, the EIP-1559 transaction's `amount` field is set to:
```
tx.amount = withdrawal_amount - max_fee_estimate
           = withdrawal_amount - (max_fee_per_gas * gas_limit)
``` [1](#0-0) 

**`transaction_amount()` accessor** (`tx.rs` lines 313–315):
```rust
pub fn transaction_amount(&self) -> &Wei {
    &self.transaction.transaction().amount  // = withdrawal_amount - max_fee_estimate
}
``` [2](#0-1) 

**Reimbursement on failure** (`mod.rs` line 727):
```rust
reimbursed_amount: finalized_tx.transaction_amount().change_units(),
// = withdrawal_amount - max_fee_estimate   ← WRONG
// should be: withdrawal_amount - effective_fee
``` [3](#0-2) 

**Correct value** is available on the same `finalized_tx` object:
```rust
pub fn effective_transaction_fee(&self) -> Wei {
    self.receipt.effective_transaction_fee()  // = effective_gas_price * gas_used
}
``` [4](#0-3) 

**Why the existing test does not catch this**: The test helper `transaction_receipt` always sets `effective_gas_price = signed_tx.transaction().max_fee_per_gas`, making `effective_fee == max_fee_estimate` and the two formulas numerically identical. The test name claims to verify "reimburse unused tx fee" but only exercises the degenerate case. [5](#0-4) [6](#0-5) 

### Impact Explanation

For every failed ckETH withdrawal where `effective_gas_price < max_fee_per_gas` (the normal case — EIP-1559 base fee is almost always below `max_fee_per_gas`):

- User burns `withdrawal_amount` ckETH.
- Minter's ETH is charged only `effective_fee` (not `max_fee_estimate`).
- User is reimbursed `withdrawal_amount - max_fee_estimate` ckETH.
- The difference `max_fee_estimate - effective_fee` is permanently lost to the user and accumulates as unaccounted ETH in the minter.

Using the concrete example from the documentation: `max_fee_estimate = 1,823,126,598,888,000 wei`, `actual_tx_fee = 899,399,014,248,000 wei`, unspent = `923,727,584,640,000 wei` (~0.00092 ETH). A user whose transaction fails at these parameters loses ~$2–3 at typical ETH prices, per failed withdrawal. [7](#0-6) 

### Likelihood Explanation

- Any unprivileged user who initiates a ckETH withdrawal that fails on Ethereum (e.g., out-of-gas, contract revert, network congestion causing revert) is affected.
- `effective_gas_price < max_fee_per_gas` is the normal case under EIP-1559 — the minter deliberately sets `max_fee_per_gas = 2 * base_fee + priority_fee` as a conservative upper bound.
- No special attacker capability is required; the loss is automatic and systematic.

### Recommendation

In `record_finalized_transaction`, replace `finalized_tx.transaction_amount()` with `withdrawal_amount - effective_fee`:

```rust
// Current (wrong):
reimbursed_amount: finalized_tx.transaction_amount().change_units(),

// Correct:
reimbursed_amount: request.withdrawal_amount
    .checked_sub(finalized_tx.effective_transaction_fee())
    .unwrap_or(Wei::ZERO)
    .change_units(),
```

Also add a test with `effective_gas_price < max_fee_per_gas` to cover the non-degenerate case. [3](#0-2) 

### Proof of Concept

Invariant that is violated:

```
reimbursed_amount + effective_fee == withdrawal_amount
```

Actual behavior:

```
reimbursed_amount + effective_fee
= (withdrawal_amount - max_fee_estimate) + effective_fee
= withdrawal_amount - (max_fee_estimate - effective_fee)
< withdrawal_amount   when effective_gas_price < max_fee_per_gas
```

Concrete numbers (from `cketh.adoc` example, failure scenario):
- `withdrawal_amount` = 39,998,000,000,000,000 wei
- `max_fee_estimate` = 1,823,126,598,888,000 wei
- `effective_fee` = 899,399,014,248,000 wei
- **Actual reimbursement**: 38,174,873,401,112,000 wei
- **Correct reimbursement**: 39,098,600,985,752,000 wei
- **User loss**: 923,727,584,640,000 wei (~0.00092 ETH) [3](#0-2) [2](#0-1)

### Citations

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L719-731)
```rust
            WithdrawalRequest::CkEth(request) => {
                if receipt.status == TransactionStatus::Failure {
                    self.record_reimbursement_request(
                        index,
                        ReimbursementRequest {
                            ledger_burn_index,
                            to: request.from,
                            to_subaccount: request.from_subaccount.clone(),
                            reimbursed_amount: finalized_tx.transaction_amount().change_units(),
                            transaction_hash: Some(receipt.transaction_hash),
                        },
                    );
                }
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1123-1145)
```rust
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

**File:** rs/ethereum/cketh/minter/src/tx.rs (L313-315)
```rust
    pub fn transaction_amount(&self) -> &Wei {
        &self.transaction.transaction().amount
    }
```

**File:** rs/ethereum/cketh/minter/src/tx.rs (L333-335)
```rust
    pub fn effective_transaction_fee(&self) -> Wei {
        self.receipt.effective_transaction_fee()
    }
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/tests.rs (L1689-1738)
```rust
        #[test]
        fn should_record_finalized_transaction_and_reimburse_unused_tx_fee_when_cketh_withdrawal_fails()
         {
            let mut transactions = EthTransactions::new(TransactionNonce::ZERO);
            let withdrawal_request = cketh_withdrawal_request_with_index(LedgerBurnIndex::new(15));
            transactions.record_withdrawal_request(withdrawal_request.clone());
            let cketh_ledger_burn_index = withdrawal_request.ledger_burn_index;
            let created_tx = create_and_record_transaction(
                &mut transactions,
                withdrawal_request.clone(),
                gas_fee_estimate(),
            );
            let signed_tx = create_and_record_signed_transaction(&mut transactions, created_tx);
            let maybe_reimburse_request = transactions
                .maybe_reimburse_requests_iter()
                .find(|r| r.cketh_ledger_burn_index() == cketh_ledger_burn_index)
                .expect("maybe reimburse request not found");
            assert_eq!(maybe_reimburse_request, &withdrawal_request.clone().into());

            let receipt = transaction_receipt(&signed_tx, TransactionStatus::Failure);
            transactions.record_finalized_transaction(cketh_ledger_burn_index, receipt.clone());

            let finalized_transaction = transactions
                .get_finalized_transaction(&cketh_ledger_burn_index)
                .expect("finalized tx not found");

            assert!(transactions.maybe_reimburse.is_empty());
            let cketh_reimbursement_index = ReimbursementIndex::CkEth {
                ledger_burn_index: cketh_ledger_burn_index,
            };
            let reimbursement_request = transactions
                .reimbursement_requests
                .get(&cketh_reimbursement_index)
                .expect("reimbursement request not found");
            let effective_fee_paid = finalized_transaction.effective_transaction_fee();
            assert_eq!(
                reimbursement_request,
                &ReimbursementRequest {
                    transaction_hash: Some(receipt.transaction_hash),
                    ledger_burn_index: cketh_ledger_burn_index,
                    to: withdrawal_request.from,
                    to_subaccount: withdrawal_request.from_subaccount,
                    reimbursed_amount: withdrawal_request
                        .withdrawal_amount
                        .checked_sub(effective_fee_paid)
                        .unwrap()
                        .change_units()
                }
            );
        }
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/tests.rs (L2895-2911)
```rust
fn transaction_receipt(
    signed_tx: &SignedEip1559TransactionRequest,
    status: TransactionStatus,
) -> TransactionReceipt {
    use std::str::FromStr;
    TransactionReceipt {
        block_hash: Hash::from_str(
            "0xce67a85c9fb8bc50213815c32814c159fd75160acf7cb8631e8e7b7cf7f1d472",
        )
        .unwrap(),
        block_number: BlockNumber::new(4190269),
        effective_gas_price: signed_tx.transaction().max_fee_per_gas,
        gas_used: signed_tx.transaction().gas_limit,
        status,
        transaction_hash: signed_tx.hash(),
    }
}
```

**File:** rs/ethereum/cketh/docs/cketh.adoc (L207-238)
```text
. Estimate the maximum current cost of a transaction on Ethereum, say `max_tx_fee_estimate`. This `max_tx_fee_estimate` is expected to be large enough to be valid for the few next blocks.
. Issue an Ethereum transaction (via threshold ECDSA) with the value `withdraw_amount - max_tx_fee_estimate`. This requires of course that `withdraw_amount >= max_tx_fee_estimate` and that's why we currently have a conservative minimum value for withdrawals of `30_000_000_000_000_000` wei. This ensures that the minter can always send the transaction to Ethereum if one or several resubmissions are needed if the Ethereum network is congested and fees are increasing rapidly (each resubmission requires an increase of at least 10% of the transaction fee).
. When the transaction is mined, the destination of the transaction will receive `withdraw_amount - max_tx_fee_estimate`. Since on Ethereum transactions are paid by the sender, the minter’s account will be charged with
+
----
(withdraw_amount - max_tx_fee_estimate) + actual_tx_fee == withdrawal_amount - (max_tx_fee_estimate - actual_tx_fee),
----
where `actual_tx_fee` represents the actual transaction fee (can be retrieved from the transaction receipt) and by construction `max_tx_fee_estimate - actual_tx_fee > 0`.

[TIP]
.Effective transaction fees vs unspent transaction fees
====
The minter dashboard displays in the metadata table the following fees

. `Total effective transaction fees`: the sum of all `actual_tx_fee` for all withdrawals.
. `Total unspent transaction fees`: the sum of all `max_tx_fee_estimate - actual_tx_fee` for all withdrawals. This represents an overestimate of the actual transaction fees that were charged to the user but in retrospect not needed to mine the sent transaction.
====

.Transaction https://etherscan.io/tx/0x5ab62cfd3715c549fb4cd56fc511bc403f45c43b1e91ffdb83654201b0b5db39[0x5ab62cfd3715c549fb4cd56fc511bc403f45c43b1e91ffdb83654201b0b5db39]
====
To make things more concrete, we break down the cost of a concrete withdrawal (ledger burn index `2`) that resulted in the Ethereum transaction https://etherscan.io/tx/0x5ab62cfd3715c549fb4cd56fc511bc403f45c43b1e91ffdb83654201b0b5db39[0x5ab62cfd3715c549fb4cd56fc511bc403f45c43b1e91ffdb83654201b0b5db39]:

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
