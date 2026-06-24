### Title
Unspent ckETH Transaction Fees Permanently Retained by Minter After ckERC20 Withdrawal — (File: `rs/ethereum/cketh/minter/src/main.rs`)

---

### Summary

When a user calls `withdraw_erc20` on the ckETH minter, the minter burns an overestimated `erc20_tx_fee` worth of ckETH from the user's account as a gas fee prepayment. After the Ethereum transaction is finalized, the actual gas fee is typically less than the burned amount. The difference — the `unspent_tx_fee` — is permanently retained in the minter's ETH balance and is never reimbursed to the user. This is the direct IC analog of the reported pattern: a user provides more tokens than are actually consumed by the operation, and the excess is silently taken.

---

### Finding Description

In `withdraw_erc20` in `rs/ethereum/cketh/minter/src/main.rs`, the minter:

1. Calls `estimate_erc20_transaction_fee()` to obtain `erc20_tx_fee` — a deliberately overestimated maximum fee that includes a safety margin for gas price fluctuations and potential transaction resubmissions.
2. Burns exactly `erc20_tx_fee` ckETH from the user's account via `cketh_ledger.burn_from(cketh_account, erc20_tx_fee, ...)`.
3. Records `max_transaction_fee: erc20_tx_fee` in the `Erc20WithdrawalRequest`. [1](#0-0) 

After the Ethereum transaction is finalized, `update_balance_upon_withdrawal` in `rs/ethereum/cketh/minter/src/state.rs` computes:

- `charged_tx_fee = req.max_transaction_fee` (the full burned ckETH amount)
- `unspent_tx_fee = charged_tx_fee - actual_tx_fee` (the excess) [2](#0-1) 

The `unspent_tx_fee` is added to the minter's internal `total_unspent_tx_fees` accounting metric but is **never minted back to the user**. The minter's ETH balance is only debited by `actual_tx_fee`, meaning the difference accrues to the minter.

The documentation explicitly acknowledges this behavior:

> "Overcharged transaction fees are not reimbursed." [3](#0-2) 

This mirrors the original vulnerability exactly: the code checks that the user has provided *at least* the minimum required amount (the minter checks `actual_min_max_fee_per_gas > request_max_fee_per_gas` to reject insufficient fees), but does not return the excess when the actual cost is lower. [4](#0-3) 

---

### Impact Explanation

**Vulnerability class: chain-fusion mint/burn/replay bug — ledger conservation.**

Every successful ckERC20 withdrawal results in a systematic, per-transaction loss of ckETH for the user equal to `max_transaction_fee - actual_tx_fee`. Because the fee estimate includes a deliberate safety margin (to allow for resubmissions at up to 10% higher gas prices), the actual fee is almost always less than the burned amount. The excess ckETH is permanently destroyed from the user's perspective — it is burned on the ckETH ledger but the corresponding ETH is never spent on Ethereum, so it accumulates as an unaccounted surplus in the minter's ETH balance (`total_unspent_tx_fees`).

This is a direct financial loss to users: they burn more ckETH than the operation actually costs, with no mechanism to recover the difference.

**Impact: High** — real, irreversible token loss on every ckERC20 withdrawal.

---

### Likelihood Explanation

This condition is triggered on **every** successful ckERC20 withdrawal where the actual Ethereum gas price at mining time is lower than the estimated maximum. Given that the fee estimate is intentionally conservative (includes a safety margin), this occurs on the vast majority of withdrawals. Any unprivileged user calling `withdraw_erc20` is affected.

**Likelihood: High** — occurs systematically on every normal withdrawal.

---

### Recommendation

After a ckERC20 Ethereum transaction is finalized, the minter should mint `unspent_tx_fee` ckETH back to the user's account (the same `cketh_account` that was charged). This is already the pattern used for failed ckERC20 withdrawals (where the full `erc20_tx_fee` minus a ledger transfer fee is reimbursed via `process_reimbursement`). The same reimbursement path should be extended to cover the unspent portion of successful withdrawals. [5](#0-4) 

---

### Proof of Concept

1. User approves the ckETH minter to spend ckETH via `icrc2_approve`.
2. User calls `withdraw_erc20(amount=1_000_000 USDC, ckerc20_ledger_id=..., recipient="0x...")`.
3. Minter calls `estimate_erc20_transaction_fee()` → returns `erc20_tx_fee = 32_500_000_000_000_000` wei (example from test at line 1611 of `rs/ethereum/cketh/minter/src/state/transactions/tests.rs`).
4. Minter burns `32_500_000_000_000_000` wei ckETH from user.
5. Ethereum transaction is mined; actual fee = `gas_used * effective_gas_price = 65_000 * 250_000_000_000 = 16_250_000_000_000_000` wei.
6. `unspent_tx_fee = 32_500_000_000_000_000 - 16_250_000_000_000_000 = 16_250_000_000_000_000` wei ckETH (~0.016 ETH) is permanently lost by the user.
7. The minter records this in `total_unspent_tx_fees` but never returns it. [6](#0-5)

### Citations

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

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L275-275)
```text
. The minter retrieves the receipt of the finalized transaction (as done currently by the ckETH minter) and will reimburse the ckERC20 tokens in case the transaction failed. Overcharged transaction fees are not reimbursed.
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

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L46-141)
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
            Ok(Ok(block_index)) => block_index
                .0
                .to_u64()
                .expect("block index should fit into u64"),
            Ok(Err(err)) => {
                log!(INFO, "[process_reimbursement] Failed to mint ckETH {err}");
                error_count += 1;
                // minting failed, defuse guard
                ScopeGuard::into_inner(prevent_double_minting_guard);
                continue;
            }
            Err(err) => {
                log!(
                    INFO,
                    "[process_reimbursement] Failed to send a message to the ledger ({ledger_canister_id}): {err:?}"
                );
                error_count += 1;
                // minting failed, defuse guard
                ScopeGuard::into_inner(prevent_double_minting_guard);
                continue;
            }
        };
        let reimbursed = Reimbursed {
            burn_in_block: reimbursement_request.ledger_burn_index,
            reimbursed_in_block: LedgerMintIndex::new(block_index),
            reimbursed_amount: reimbursement_request.reimbursed_amount,
            transaction_hash: reimbursement_request.transaction_hash,
        };
        let event = match index {
            ReimbursementIndex::CkEth {
                ledger_burn_index: _,
            } => EventType::ReimbursedEthWithdrawal(reimbursed),
            ReimbursementIndex::CkErc20 {
                cketh_ledger_burn_index,
                ledger_id,
                ckerc20_ledger_burn_index: _,
            } => EventType::ReimbursedErc20Withdrawal {
                cketh_ledger_burn_index,
                ckerc20_ledger_id: ledger_id,
                reimbursed,
            },
        };
        mutate_state(|s| process_event(s, event));
        // minting succeeded, defuse guard
        ScopeGuard::into_inner(prevent_double_minting_guard);
    }
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/tests.rs (L1605-1639)
```rust
        #[test]
        fn should_not_reimburse_when_ckerc20_witdrawal_used_up_transaction_fee() {
            let mut transactions = EthTransactions::new(TransactionNonce::ZERO);
            let cketh_ledger_burn_index = LedgerBurnIndex::new(7);
            let ckerc20_ledger_burn_index = LedgerBurnIndex::new(7);
            let withdrawal_request = Erc20WithdrawalRequest {
                max_transaction_fee: Wei::from(32_500_000_000_000_000_u128),
                ..ckerc20_withdrawal_request_with_index(
                    cketh_ledger_burn_index,
                    ckerc20_ledger_burn_index,
                )
            };
            transactions.record_withdrawal_request(withdrawal_request.clone());
            let created_tx = create_and_record_transaction(
                &mut transactions,
                withdrawal_request.clone(),
                GasFeeEstimate {
                    base_fee_per_gas: WeiPerGas::from(250_000_000_000_u128),
                    max_priority_fee_per_gas: WeiPerGas::ZERO,
                },
            );
            let signed_tx = create_and_record_signed_transaction(&mut transactions, created_tx);
            let receipt = TransactionReceipt {
                gas_used: GasAmount::from(65_000_u32),
                effective_gas_price: WeiPerGas::from(500_000_000_000_u128),
                ..transaction_receipt(&signed_tx, TransactionStatus::Success)
            };
            assert_eq!(
                receipt.effective_transaction_fee(),
                withdrawal_request.max_transaction_fee
            );
            transactions.record_finalized_transaction(cketh_ledger_burn_index, receipt.clone());

            assert_eq!(transactions.maybe_reimburse, btreeset! {});
            assert_eq!(transactions.reimbursement_requests, btreemap! {});
```
