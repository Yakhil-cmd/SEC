### Title
ckERC20 `withdraw_erc20` Burns Full `max_transaction_fee` in ckETH But Never Reimburses the Unspent Portion After Transaction Finalization - (`rs/ethereum/cketh/minter/src/main.rs`)

---

### Summary

When a user calls `withdraw_erc20` on the ckETH minter, the full estimated `max_transaction_fee` is burned from the user's ckETH balance upfront. After the Ethereum transaction is finalized, the actual fee paid (`actual_tx_fee`) is always less than `max_transaction_fee` by design. The difference — `unspent_tx_fee = max_transaction_fee - actual_tx_fee` — is tracked internally but **never reimbursed to the user**. It accumulates in the minter's ETH balance. This is structurally identical to H-05: a maximum amount is taken from the user, only a portion is consumed, and the remainder is silently retained.

---

### Finding Description

**Step 1 — Upfront burn of `max_transaction_fee`:**

In `withdraw_erc20` (`rs/ethereum/cketh/minter/src/main.rs`), the minter calls `estimate_erc20_transaction_fee()` to compute `erc20_tx_fee`, then immediately burns the full amount from the caller's ckETH account:

```rust
let erc20_tx_fee = estimate_erc20_transaction_fee().await...;
match cketh_ledger
    .burn_from(cketh_account, erc20_tx_fee, BurnMemo::Erc20GasFee { ... })
    .await
``` [1](#0-0) 

The fee estimate is deliberately conservative: `max_fee_per_gas = 2 * base_fee_per_gas + max_priority_fee_per_gas`, ensuring validity for the next few blocks. [2](#0-1) 

**Step 2 — `max_transaction_fee` stored in the withdrawal request:**

The full `erc20_tx_fee` is stored as `max_transaction_fee` in the `Erc20WithdrawalRequest`:

```rust
let withdrawal_request = Erc20WithdrawalRequest {
    max_transaction_fee: erc20_tx_fee,
    ...
};
``` [3](#0-2) 

**Step 3 — Unspent fee computed but never returned:**

After the Ethereum transaction is finalized, `update_balance_upon_withdrawal` computes the unspent fee:

```rust
let charged_tx_fee = match withdrawal_request {
    WithdrawalRequest::CkErc20(req) => req.max_transaction_fee,  // full amount burned
    ...
};
let unspent_tx_fee = charged_tx_fee.checked_sub(tx_fee)...;
self.eth_balance.total_unspent_tx_fees_add(unspent_tx_fee);  // tracked, not returned
``` [4](#0-3) 

The `unspent_tx_fee` is only added to a metrics counter. No reimbursement request is created for the user.

**Step 4 — Confirmed by test and documentation:**

The test `should_not_reimburse_unused_transaction_fee_when_ckerc20_withdrawal_successful` explicitly asserts that `reimbursement_requests` is empty even when `actual_tx_fee < max_transaction_fee`: [5](#0-4) 

The documentation explicitly acknowledges this behavior:

> "Overcharged transaction fees are not reimbursed." [6](#0-5) 

**Contrast with ckETH withdrawals:**

For plain ckETH withdrawals (`withdraw_eth`), the unspent fee is implicitly absorbed because the transaction amount is set to `withdrawal_amount - max_tx_fee_estimate`, so the user's ETH destination receives less but the user's ckETH burn is exact. For ckERC20, the user burns a separate ckETH amount for fees, and the ERC20 amount is transferred in full — making the unspent ckETH fee a direct, unrecovered loss. [7](#0-6) 

---

### Impact Explanation

Every user who successfully withdraws ckERC20 tokens loses `max_transaction_fee - actual_tx_fee` ckETH permanently. Since `max_fee_per_gas` is set to `2 * base_fee_per_gas + max_priority_fee_per_gas`, the overestimate is structural and consistent. The minter's own documentation quantifies a concrete example where `unspent_tx_fee = 923,727,584,640,000 wei` (~0.00092 ETH) was silently retained from a single ckETH withdrawal. For ckERC20 withdrawals, the same overestimate applies to the separately burned ckETH fee, and the user receives no refund. The accumulated `total_unspent_tx_fees` in the minter's ETH balance represents funds taken from users but never returned. [8](#0-7) 

---

### Likelihood Explanation

This affects **every successful ckERC20 withdrawal** without exception. The fee overestimate is by design (to guarantee transaction inclusion), so `actual_tx_fee < max_transaction_fee` is the normal case. Any unprivileged user calling `withdraw_erc20` on the ckETH minter canister triggers this behavior. No special conditions, timing, or attacker capability is required. [9](#0-8) 

---

### Recommendation

After the Ethereum transaction is finalized with `TransactionStatus::Success`, compute `unspent_tx_fee = max_transaction_fee - actual_tx_fee` and create a `ReimbursementRequest` to mint the unspent ckETH back to the user, analogous to how failed ckETH withdrawals are reimbursed via `process_reimbursement`. The existing reimbursement infrastructure already supports this pattern: [10](#0-9) 

The fix for the ckERC20 success path would mirror the existing failure-path reimbursement for ckERC20 tokens: [11](#0-10) 

---

### Proof of Concept

1. User approves the ckETH minter to spend `max_transaction_fee` ckETH (e.g., 5,000,000,000,000,000 wei).
2. User approves the ckERC20 minter to spend `amount` ckUSDC.
3. User calls `withdraw_erc20(amount, ckerc20_ledger_id, eth_address)`.
4. Minter burns `max_transaction_fee` ckETH from user (e.g., 5,000,000,000,000,000 wei).
5. Ethereum transaction is mined; `actual_tx_fee` = 3,000,000,000,000,000 wei.
6. User's ckUSDC arrives at `eth_address`. ✓
7. User's ckETH balance is reduced by 5,000,000,000,000,000 wei, not 3,000,000,000,000,000 wei.
8. The 2,000,000,000,000,000 wei difference is added to `total_unspent_tx_fees` in the minter state and never returned.

```
// Confirmed by:
assert_eq!(transactions.reimbursement_requests, btreemap! {});
// (should_not_reimburse_unused_transaction_fee_when_ckerc20_withdrawal_successful)
``` [12](#0-11)

### Citations

**File:** rs/ethereum/cketh/minter/src/main.rs (L389-398)
```rust
#[update]
async fn withdraw_erc20(
    WithdrawErc20Arg {
        amount,
        ckerc20_ledger_id,
        recipient,
        from_cketh_subaccount,
        from_ckerc20_subaccount,
    }: WithdrawErc20Arg,
) -> Result<RetrieveErc20Request, WithdrawErc20Error> {
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L430-458)
```rust
    let erc20_tx_fee = estimate_erc20_transaction_fee().await.ok_or_else(|| {
        WithdrawErc20Error::TemporarilyUnavailable("Failed to retrieve current gas fee".to_string())
    })?;
    let cketh_account = Account {
        owner: caller,
        subaccount: from_cketh_subaccount,
    };
    let ckerc20_account = Account {
        owner: caller,
        subaccount: from_ckerc20_subaccount,
    };
    let now = ic_cdk::api::time();
    log!(
        INFO,
        "[withdraw_erc20]: burning {:?} ckETH from account {}",
        erc20_tx_fee,
        cketh_account
    );
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

**File:** rs/ethereum/cketh/minter/src/tx.rs (L517-536)
```rust
    pub fn checked_estimate_max_fee_per_gas(&self) -> Option<WeiPerGas> {
        self.base_fee_per_gas
            .checked_mul(2_u8)
            .and_then(|base_fee_estimate| {
                base_fee_estimate.checked_add(self.max_priority_fee_per_gas)
            })
    }

    pub fn estimate_max_fee_per_gas(&self) -> WeiPerGas {
        self.checked_estimate_max_fee_per_gas()
            .unwrap_or(WeiPerGas::MAX)
    }

    pub fn to_price(self, gas_limit: GasAmount) -> TransactionPrice {
        TransactionPrice {
            gas_limit,
            max_fee_per_gas: self.estimate_max_fee_per_gas(),
            max_priority_fee_per_gas: self.max_priority_fee_per_gas,
        }
    }
```

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

**File:** rs/ethereum/cketh/minter/src/state/transactions/tests.rs (L1574-1603)
```rust
        #[test]
        fn should_not_reimburse_unused_transaction_fee_when_ckerc20_withdrawal_successful() {
            let mut transactions = EthTransactions::new(TransactionNonce::ZERO);
            let cketh_ledger_burn_index = LedgerBurnIndex::new(7);
            let ckerc20_ledger_burn_index = LedgerBurnIndex::new(7);
            let withdrawal_request = ckerc20_withdrawal_request_with_index(
                cketh_ledger_burn_index,
                ckerc20_ledger_burn_index,
            );
            transactions.record_withdrawal_request(withdrawal_request.clone());
            let created_tx = create_and_record_transaction(
                &mut transactions,
                withdrawal_request.clone(),
                gas_fee_estimate(),
            );
            let signed_tx = create_and_record_signed_transaction(&mut transactions, created_tx);
            let receipt = TransactionReceipt {
                gas_used: GasAmount::from(40_000_u32),
                effective_gas_price: WeiPerGas::from(100_u16),
                ..transaction_receipt(&signed_tx, TransactionStatus::Success)
            };
            assert_eq!(
                receipt.effective_transaction_fee(),
                Wei::from(4_000_000_u32)
            );
            transactions.record_finalized_transaction(cketh_ledger_burn_index, receipt.clone());

            assert_eq!(transactions.maybe_reimburse, btreeset! {});
            assert_eq!(transactions.reimbursement_requests, btreemap! {});
        }
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/tests.rs (L1642-1687)
```rust
        #[test]
        fn should_reimburse_tokens_when_ckerc20_withdrawal_fails() {
            let mut transactions = EthTransactions::new(TransactionNonce::ZERO);
            let cketh_ledger_burn_index = LedgerBurnIndex::new(7);
            let ckerc20_ledger_burn_index = LedgerBurnIndex::new(7);
            let withdrawal_request = ckerc20_withdrawal_request_with_index(
                cketh_ledger_burn_index,
                ckerc20_ledger_burn_index,
            );
            transactions.record_withdrawal_request(withdrawal_request.clone());
            let created_tx = create_and_record_transaction(
                &mut transactions,
                withdrawal_request.clone(),
                gas_fee_estimate(),
            );
            let signed_tx = create_and_record_signed_transaction(&mut transactions, created_tx);
            let receipt = TransactionReceipt {
                gas_used: GasAmount::from(40_000_u32),
                effective_gas_price: WeiPerGas::from(100_u16),
                ..transaction_receipt(&signed_tx, TransactionStatus::Failure)
            };
            assert_eq!(
                receipt.effective_transaction_fee(),
                Wei::from(4_000_000_u32)
            );
            transactions.record_finalized_transaction(cketh_ledger_burn_index, receipt.clone());
            let expected_ckerc20_reimbursed_amount = withdrawal_request.withdrawal_amount;

            assert_eq!(transactions.maybe_reimburse, btreeset! {});
            assert_eq!(
                transactions.reimbursement_requests,
                btreemap! {
                    ReimbursementIndex::CkErc20 {
                        cketh_ledger_burn_index,
                        ledger_id: withdrawal_request.ckerc20_ledger_id,
                        ckerc20_ledger_burn_index } =>
                    ReimbursementRequest {
                        ledger_burn_index: cketh_ledger_burn_index,
                        reimbursed_amount: expected_ckerc20_reimbursed_amount.change_units(),
                        to: withdrawal_request.from,
                        to_subaccount: withdrawal_request.from_subaccount,
                        transaction_hash: Some(receipt.transaction_hash),
                    }
                }
            );
        }
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L275-275)
```text
. The minter retrieves the receipt of the finalized transaction (as done currently by the ckETH minter) and will reimburse the ckERC20 tokens in case the transaction failed. Overcharged transaction fees are not reimbursed.
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

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L46-95)
```rust
pub async fn process_reimbursement() {
    let _guard = match TimerGuard::new(TaskType::Reimbursement) {
        Ok(guard) => guard,
        Err(e) => {
            log!(DEBUG, "Failed retrieving reimbursement guard: {e:?}",);
            return;
        }
    };

    let reimbursements: Vec<(ReimbursementIndex, ReimbursementRequest)> = read_state(|s| {
        s.eth_transactions
            .reimbursement_requests_iter()
            .map(|(index, request)| (index.clone(), request.clone()))
            .collect()
    });
    if reimbursements.is_empty() {
        return;
    }

    let mut error_count = 0;

    for (index, reimbursement_request) in reimbursements {
        // Ensure that even if we were to panic in the callback, after having contacted the ledger to mint the tokens,
        // this reimbursement request will not be processed again.
        let prevent_double_minting_guard = scopeguard::guard(index.clone(), |index| {
            mutate_state(|s| process_event(s, EventType::QuarantinedReimbursement { index }));
        });
        let ledger_canister_id = match index {
            ReimbursementIndex::CkEth { .. } => read_state(|s| s.cketh_ledger_id),
            ReimbursementIndex::CkErc20 { ledger_id, .. } => ledger_id,
        };
        let client = ICRC1Client {
            runtime: CdkRuntime,
            ledger_canister_id,
        };
        let memo = Memo::from(reimbursement_request.clone());
        let args = TransferArg {
            from_subaccount: None,
            to: Account {
                owner: reimbursement_request.to,
                subaccount: reimbursement_request
                    .to_subaccount
                    .map(LedgerSubaccount::to_bytes),
            },
            fee: None,
            created_at_time: None,
            memo: Some(memo),
            amount: Nat::from(reimbursement_request.reimbursed_amount),
        };
        let block_index = match client.transfer(args).await {
```
