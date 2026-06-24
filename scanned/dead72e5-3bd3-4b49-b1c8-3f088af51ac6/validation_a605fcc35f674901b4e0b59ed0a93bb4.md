### Title
Overcharged ckETH Transaction Fees Permanently Locked in Minter on Successful ckERC20 Withdrawals - (File: rs/ethereum/cketh/minter/src/state/transactions/mod.rs)

### Summary
When a user performs a ckERC20 → ERC20 withdrawal via the ckETH minter, they pre-pay a `max_transaction_fee` in ckETH. On a **successful** withdrawal, the difference between the pre-paid maximum fee and the actual Ethereum gas cost (the "unspent" fee) is **never reimbursed** to the user and is permanently retained by the minter canister. This is an intentional design choice explicitly documented, but it constitutes a ledger conservation bug analogous to the reported issue: tokens deposited to enable a conversion are locked forever with no redemption path.

### Finding Description

The ckERC20 withdrawal flow works as follows:

1. The user calls `withdraw_erc20` on the ckETH minter.
2. The minter burns `erc20_tx_fee` (a `max_transaction_fee` estimate) from the user's ckETH balance.
3. The minter burns the ckERC20 amount from the user's ckERC20 balance.
4. The minter submits an Ethereum transaction and pays the actual gas cost from its own ETH balance.
5. On success, the minter keeps the difference `max_transaction_fee - actual_tx_fee` (the unspent fee).

The code in `rs/ethereum/cketh/minter/src/state/transactions/mod.rs` explicitly tracks this as `total_unspent_tx_fees` and the documentation in `rs/ethereum/cketh/docs/ckerc20.adoc` line 275 states:

> "Overcharged transaction fees are not reimbursed."

The test `should_not_reimburse_unused_transaction_fee_when_ckerc20_withdrawal_successful` in `rs/ethereum/cketh/minter/src/state/transactions/tests.rs` confirms that on a successful ckERC20 withdrawal with `effective_transaction_fee < max_transaction_fee`, `reimbursement_requests` remains empty — the surplus ckETH is never returned.

This contrasts with the ckETH (non-ERC20) withdrawal path, where the user's withdrawal amount is reduced by the actual fee at transaction creation time (the user only pays what is actually used), and with the failed ckERC20 withdrawal path, where the ckERC20 tokens are reimbursed.

### Impact Explanation

Every successful ckERC20 withdrawal permanently destroys a portion of the user's ckETH that was pre-burned as `max_transaction_fee` but not consumed as actual gas. The minter's ETH balance on Ethereum is only debited by the actual gas cost, while the user's ckETH ledger is debited by the full `max_transaction_fee`. The surplus ckETH (`max_transaction_fee - actual_tx_fee`) is burned from the ckETH ledger but the corresponding ETH remains in the minter's Ethereum address — permanently inaccessible to the user. This breaks the 1:1 backing invariant of ckETH: more ckETH is burned than ETH is spent, meaning the minter accumulates excess ETH that no ckETH holder can ever redeem.

### Likelihood Explanation

This affects **every** successful ckERC20 withdrawal. The gas fee estimate includes a safety margin (at least 10% above current fees to allow resubmission), so in practice `actual_tx_fee < max_transaction_fee` on virtually every successful transaction. Any unprivileged user calling `withdraw_erc20` triggers this path. The ckETH minter is a production canister handling real USDC, USDT, WBTC, and other ERC-20 tokens on Ethereum mainnet.

### Recommendation

On a successful ckERC20 withdrawal, compute `unspent_fee = max_transaction_fee - actual_tx_fee` and schedule a reimbursement of `unspent_fee` (minus the ckETH ledger transfer fee) back to the user's ckETH account, using the same `process_reimbursement` mechanism already in place for failed withdrawals. This mirrors how the ckETH (non-ERC20) withdrawal path already handles fee overestimates by only deducting the actual fee from the sent amount.

### Proof of Concept

**Root cause — no reimbursement on success:** [1](#0-0) 

The test `should_not_reimburse_unused_transaction_fee_when_ckerc20_withdrawal_successful` explicitly asserts `reimbursement_requests` is empty after a successful withdrawal where `effective_transaction_fee` (4,000,000 wei) is far below `max_transaction_fee`.

**Unspent fee is tracked but never returned:** [2](#0-1) 

`unspent_tx_fee` is computed and added to `total_unspent_tx_fees` as an accounting metric, but no reimbursement request is created for the ckERC20 success case.

**Contrast: failed ckERC20 withdrawal does reimburse ckERC20 tokens:** [3](#0-2) 

**Documentation explicitly acknowledges the locked funds:** [4](#0-3) 

> "Overcharged transaction fees are not reimbursed."

**Entry path — any unprivileged user can trigger this:** [5](#0-4) 

Any non-anonymous principal can call `withdraw_erc20`, burn ckETH as `max_transaction_fee`, and upon success lose the unspent portion permanently.

**The ckETH-only withdrawal path does NOT have this problem** — it deducts the fee from the sent amount at transaction creation time, so the user only pays what is actually used: [6](#0-5)

### Citations

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

**File:** rs/ethereum/cketh/minter/src/state.rs (L362-375)
```rust
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

**File:** rs/ethereum/cketh/minter/src/main.rs (L389-428)
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
    let ckerc20_withdrawal_amount =
        Erc20Value::try_from(amount).expect("ERROR: failed to convert Nat to u256");

    let ckerc20_token = read_state(|s| s.find_ck_erc20_token_by_ledger_id(&ckerc20_ledger_id))
        .ok_or_else(|| {
            let supported_ckerc20_tokens: BTreeSet<_> = read_state(|s| {
                s.supported_ck_erc20_tokens()
                    .map(|token| token.into())
                    .collect()
            });
            WithdrawErc20Error::TokenNotSupported {
                supported_tokens: Vec::from_iter(supported_ckerc20_tokens),
            }
        })?;
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
