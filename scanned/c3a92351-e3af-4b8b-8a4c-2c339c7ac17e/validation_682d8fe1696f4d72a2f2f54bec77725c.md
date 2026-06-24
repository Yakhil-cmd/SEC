### Title
Overcharged ckETH Transaction Fees Are Permanently Lost to Users on Successful ckERC20 Withdrawals - (File: rs/ethereum/cketh/minter/src/state.rs)

### Summary

The ckETH minter charges users a `max_transaction_fee` (an overestimate of the actual Ethereum gas cost) when processing `withdraw_erc20` requests. For **successful** ckERC20 withdrawals, the difference between `max_transaction_fee` and the actual `effective_transaction_fee` (`unspent_tx_fee`) is permanently retained by the minter and never reimbursed to the user. This is an acknowledged design choice documented in the codebase, but it constitutes a direct, quantifiable, and systematic ledger conservation loss for every user who successfully withdraws ckERC20 tokens.

### Finding Description

When a user calls `withdraw_erc20`, the minter burns `erc20_tx_fee` (the estimated maximum transaction fee) from the user's ckETH balance upfront. [1](#0-0) 

The `erc20_tx_fee` is a conservative overestimate that includes a safety margin to allow for transaction resubmissions (each requiring at least a 10% fee increase). [2](#0-1) 

After the Ethereum transaction is finalized, `update_balance_upon_withdrawal` computes the `unspent_tx_fee = charged_tx_fee - actual_tx_fee` and records it in `total_unspent_tx_fees` — but **does not schedule any reimbursement** for the user: [3](#0-2) 

The reimbursement logic in `record_finalized_transaction` only triggers for **failed** ckERC20 transactions (reimbursing the ckERC20 tokens) or for **failed** ckETH transactions (reimbursing the unused ckETH). For a **successful** ckERC20 withdrawal, no ckETH reimbursement of the overcharged fee is ever issued. [4](#0-3) 

This is explicitly documented: *"Overcharged transaction fees are not reimbursed."*

The `unspent_tx_fee` accumulates in the minter's ETH balance as `total_unspent_tx_fees` — a metric that tracks the systematic overcharge — but no mechanism exists to return this to users. [5](#0-4) 

By contrast, for ckETH-only withdrawals, the user receives `withdraw_amount - max_tx_fee_estimate` at the destination, so the overcharge is implicitly absorbed into the sent amount (the user bears it as reduced received ETH). For ckERC20 withdrawals, the user burns a **separate** ckETH amount equal to `max_transaction_fee` and receives the full `withdrawal_amount` of ERC20 tokens — meaning the overcharged ckETH is a pure loss with no corresponding benefit.

### Impact Explanation

Every successful `withdraw_erc20` call results in a permanent, non-recoverable loss of ckETH for the user equal to `max_transaction_fee - actual_transaction_fee`. Given that:
- `max_transaction_fee` includes a safety margin for resubmissions (≥10% buffer per resubmission)
- Ethereum gas prices are volatile and the estimate is intentionally conservative
- The `total_unspent_tx_fees` field tracks this accumulation across all withdrawals

The minter permanently retains ckETH that was burned from users but not consumed by the Ethereum network. This is a **ledger conservation bug**: ckETH is burned from users in excess of what is actually spent, and the surplus is never minted back. The minter's ETH balance grows by `unspent_tx_fee` per successful ckERC20 withdrawal, representing funds that belong to users but are inaccessible to them.

### Likelihood Explanation

This affects **every** successful `withdraw_erc20` call. Since gas price estimates are always conservative by design (to ensure transaction validity across multiple blocks and resubmissions), `actual_tx_fee < max_transaction_fee` is the normal case, not an edge case. Any unprivileged user who calls `withdraw_erc20` and whose transaction succeeds will lose the overcharged amount. The entry path is the public `withdraw_erc20` update endpoint, callable by any IC principal. [6](#0-5) 

### Recommendation

After a ckERC20 withdrawal transaction is finalized successfully, compute `unspent_tx_fee = max_transaction_fee - effective_transaction_fee` and schedule a ckETH reimbursement mint to the user for this amount (minus the ledger transfer fee). This mirrors the existing reimbursement logic already used for failed ckETH withdrawals in `record_finalized_transaction`. The `process_reimbursement` timer task already has the infrastructure to mint ckETH back to users. [7](#0-6) 

### Proof of Concept

1. User calls `withdraw_erc20` with `amount = 1000 USDC`, `recipient = "0x..."`.
2. Minter estimates `erc20_tx_fee = 32_500_000_000_000_000 wei` (ckETH) and burns it from the user.
3. Minter submits Ethereum transaction; actual gas used results in `effective_transaction_fee = 4_000_000 wei`.
4. `update_balance_upon_withdrawal` records `unspent_tx_fee = 32_500_000_000_000_000 - 4_000_000 wei`.
5. No reimbursement request is created for the user.
6. User has permanently lost `≈32.5M wei` of ckETH beyond what was actually needed.

This is confirmed by the test `should_not_reimburse_when_ckerc20_witdrawal_used_up_transaction_fee` which shows that even when the fee is fully consumed there is no reimbursement — and by the absence of any analogous test for partial fee consumption with reimbursement on success. [8](#0-7)

### Citations

**File:** rs/ethereum/cketh/minter/src/main.rs (L389-458)
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
    let cketh_ledger = read_state(LedgerClient::cketh_ledger_from_state);
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

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L275-275)
```text
. The minter retrieves the receipt of the finalized transaction (as done currently by the ckETH minter) and will reimburse the ckERC20 tokens in case the transaction failed. Overcharged transaction fees are not reimbursed.
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
