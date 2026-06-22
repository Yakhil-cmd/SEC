### Title
ckERC20 Withdrawal On-Chain Failure Does Not Reimburse Burned ckETH Gas Fee - (`rs/ethereum/cketh/minter/src/state/transactions/mod.rs`)

---

### Summary

When a ckERC20 → ERC-20 withdrawal transaction is submitted to Ethereum and the on-chain ERC-20 `transfer()` call reverts (i.e., the Ethereum transaction itself is mined but its status is `Failure`), the ckETH minter correctly reimburses the user's burned ckERC20 tokens. However, the ckETH gas-fee tokens that were burned at the start of the withdrawal are **never reimbursed** to the user. The user permanently loses the ckETH they paid as a gas fee even though the ERC-20 transfer never succeeded on Ethereum.

---

### Finding Description

The ckERC20 withdrawal flow in `withdraw_erc20` (`rs/ethereum/cketh/minter/src/main.rs`) performs two sequential burns before submitting the Ethereum transaction:

1. Burns `erc20_tx_fee` worth of ckETH from the user to pay for the Ethereum gas.
2. Burns `ckerc20_withdrawal_amount` of the ckERC20 token from the user. [1](#0-0) 

After the Ethereum transaction is mined, `record_finalized_transaction` in `EthTransactions` is called. For a `WithdrawalRequest::CkErc20` with `TransactionStatus::Failure`, it creates a reimbursement request **only for the ckERC20 withdrawal amount** (`request.withdrawal_amount`), not for the ckETH gas fee (`request.max_transaction_fee`): [2](#0-1) 

The `ReimbursementRequest` created here targets the ckERC20 ledger (via `ReimbursementIndex::CkErc20`) and reimburses `request.withdrawal_amount`. The ckETH burn (`cketh_ledger_burn_index`) is recorded in the index but **no separate ckETH reimbursement request is created** for the gas fee. The documentation explicitly acknowledges this:

> "The minter retrieves the receipt of the finalized transaction and will reimburse the ckERC20 tokens in case the transaction failed. **Overcharged transaction fees are not reimbursed.**" [3](#0-2) 

This is the analog of the Axelar bug: the user's funds (ckETH gas fee) are burned/locked on the IC side, the Ethereum-side transfer fails, and the user does not receive their funds back on the IC side.

The contrast with the ckETH-only withdrawal path is instructive: for `WithdrawalRequest::CkEth`, the reimbursed amount is `finalized_tx.transaction_amount()` — i.e., the withdrawal amount minus the **actual** gas consumed — so the user gets back the unused portion: [4](#0-3) 

For ckERC20, the entire `max_transaction_fee` ckETH burn is consumed with no refund path when the on-chain transaction fails.

---

### Impact Explanation

A user who initiates a ckERC20 withdrawal and whose Ethereum transaction is mined but reverts (e.g., the ERC-20 contract's `transfer()` reverts due to a blacklist, paused contract, or any other ERC-20-level revert condition) will:

- Have their ckERC20 tokens reimbursed (correct).
- **Permanently lose** the ckETH they burned as a gas fee (`max_transaction_fee`), even though the ERC-20 transfer never succeeded.

The `max_transaction_fee` for a ckERC20 withdrawal is estimated at the time of the call and can be substantial (e.g., `DEFAULT_CKERC20_WITHDRAWAL_TRANSACTION_FEE` in tests is on the order of tens of millions of wei). The user receives no compensation for this loss. [5](#0-4) 

---

### Likelihood Explanation

This is reachable by any unprivileged user who:
1. Calls `withdraw_erc20` with a supported ckERC20 token.
2. Has the resulting Ethereum transaction mined with `status = Failure` (e.g., the ERC-20 contract reverts the `transfer` call).

ERC-20 contracts can revert transfers for many reasons (token blacklists, paused state, insufficient allowance at the contract level, etc.). This is a realistic scenario for tokens like USDC/USDT which have blocklist functionality. The entry path is a standard unprivileged `withdraw_erc20` canister call. [6](#0-5) 

---

### Recommendation

In `record_finalized_transaction`, when a `WithdrawalRequest::CkErc20` transaction finalizes with `TransactionStatus::Failure`, add a second reimbursement request targeting the ckETH ledger for the unused gas fee. The reimbursed amount should be `request.max_transaction_fee - effective_transaction_fee` (mirroring the ckETH withdrawal path), so the user is refunded the portion of the gas fee not actually consumed by the failed transaction.

Concretely, the `WithdrawalRequest::CkErc20` branch in `record_finalized_transaction` should also call `record_reimbursement_request` with a `ReimbursementIndex::CkEth` entry for `cketh_ledger_burn_index` and `reimbursed_amount = request.max_transaction_fee - finalized_tx.effective_transaction_fee()`. [7](#0-6) 

---

### Proof of Concept

1. User calls `withdraw_erc20` for a USDC amount, burning `erc20_tx_fee` ckETH and `amount` ckUSDC.
2. The minter submits an Ethereum transaction calling `transfer(destination, amount)` on the USDC contract.
3. The USDC contract reverts (e.g., destination is blacklisted).
4. The Ethereum transaction is mined with `status = Failure`.
5. `finalize_transactions_batch` → `record_finalized_transaction` is called.
6. The `CkErc20` branch creates a reimbursement for `withdrawal_amount` ckUSDC only.
7. `process_reimbursement` mints back the ckUSDC to the user.
8. The ckETH burn (`cketh_ledger_burn_index`) has no corresponding reimbursement request — the ckETH is permanently lost.

The test `should_reimburse_tokens_when_ckerc20_withdrawal_fails` confirms that only ckERC20 is reimbursed and no ckETH reimbursement request is created: [8](#0-7)

### Citations

**File:** rs/ethereum/cketh/minter/src/main.rs (L389-414)
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
    validate_ckerc20_active();
    let caller = validate_caller_not_anonymous();
    let _guard = retrieve_withdraw_guard(caller).unwrap_or_else(|e| {
        ic_cdk::trap(format!(
            "Failed retrieving guard for principal {caller}: {e:?}"
        ))
    });

    let destination = validate_address_as_destination(&recipient).map_err(|e| match e {
        AddressValidationError::Invalid { .. } | AddressValidationError::NotSupported(_) => {
            ic_cdk::trap(e.to_string())
        }
        AddressValidationError::Blocked(address) => WithdrawErc20Error::RecipientAddressBlocked {
            address: address.to_string(),
        },
    })?;
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L448-504)
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
    {
        Ok(cketh_ledger_burn_index) => {
            log!(
                INFO,
                "[withdraw_erc20]: burning {} {} from account {}",
                ckerc20_withdrawal_amount,
                ckerc20_token.ckerc20_token_symbol,
                ckerc20_account
            );
            match LedgerClient::ckerc20_ledger(&ckerc20_token)
                .burn_from(
                    ckerc20_account,
                    ckerc20_withdrawal_amount,
                    BurnMemo::Erc20Convert {
                        ckerc20_withdrawal_id: cketh_ledger_burn_index.get(),
                        to_address: destination,
                    },
                )
                .await
            {
                Ok(ckerc20_ledger_burn_index) => {
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
                    log!(
                        INFO,
                        "[withdraw_erc20]: queuing withdrawal request {:?}",
                        withdrawal_request
                    );
                    mutate_state(|s| {
                        process_event(
                            s,
                            EventType::AcceptedErc20WithdrawalRequest(withdrawal_request.clone()),
                        );
                    });
                    Ok(RetrieveErc20Request::from(withdrawal_request))
```

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

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L733-748)
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
            }
        }
    }
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L275-275)
```text
. The minter retrieves the receipt of the finalized transaction (as done currently by the ckETH minter) and will reimburse the ckERC20 tokens in case the transaction failed. Overcharged transaction fees are not reimbursed.
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
