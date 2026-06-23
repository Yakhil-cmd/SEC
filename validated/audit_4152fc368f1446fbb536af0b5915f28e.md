### Title
Burned ckETH Gas Fee Unrecoverable on Successful ckERC20 Withdrawal (Overcharge Not Reimbursed) - (File: `rs/ethereum/cketh/minter/src/state/transactions/mod.rs`)

---

### Summary

In the ckETH minter's `withdraw_erc20` flow, a user must pre-burn ckETH to cover the estimated Ethereum gas fee (`max_transaction_fee`). By design, when the Ethereum transaction **succeeds**, the difference between the burned `max_transaction_fee` and the actual gas consumed (`effective_transaction_fee`) is **never reimbursed** to the user. This is an explicit, permanent loss of user funds — analogous to the original report's `WITHDRAWAL_STAKE` that cannot be recovered in `exodusMode`.

---

### Finding Description

When a user calls `withdraw_erc20`, the minter:

1. Burns `erc20_tx_fee` (the estimated max gas fee) from the user's ckETH balance.
2. Burns the ckERC20 token amount from the user's ckERC20 balance.
3. Submits an Ethereum ERC-20 transfer transaction. [1](#0-0) 

The `max_transaction_fee` is set conservatively high (with a safety margin for resubmissions) to ensure the transaction can be mined even if gas prices rise. [2](#0-1) 

When the Ethereum transaction is **finalized with success**, `record_finalized_transaction` only schedules a reimbursement if `receipt.status == TransactionStatus::Failure`. On success, **no reimbursement of the overcharged ckETH fee is issued**: [3](#0-2) 

This is explicitly confirmed in the documentation:

> "Overcharged transaction fees are not reimbursed." [4](#0-3) 

And confirmed by the test `should_not_reimburse_unused_transaction_fee_when_ckerc20_withdrawal_successful`: [5](#0-4) 

The `update_balance_upon_withdrawal` function tracks the `unspent_tx_fee` in the minter's internal accounting but does **not** return it to the user — it accrues to the minter's ETH balance: [6](#0-5) 

---

### Impact Explanation

**Impact: Medium — permanent partial loss of user funds.**

Every successful ckERC20 withdrawal results in the user losing `max_transaction_fee - actual_transaction_fee` ckETH permanently. The minter retains this difference as unspent ETH. For ckERC20 withdrawals (gas limit fixed at 65,000), the overcharge can be substantial when Ethereum gas prices are volatile. The burned ckETH is destroyed on the IC ledger; the corresponding ETH stays in the minter's Ethereum address and is never redistributed to users. This is a **ledger conservation bug**: ckETH supply is reduced by more than the ETH actually spent.

---

### Likelihood Explanation

**Likelihood: High — this occurs on every successful ckERC20 withdrawal.**

The `max_transaction_fee` is intentionally set with a safety margin (at least 10% above current estimates to allow resubmissions). In practice, the actual gas used is almost always less than the maximum. This is not an edge case — it is the normal operating path for every ckERC20 withdrawal that succeeds.

---

### Recommendation

For ckERC20 withdrawals, after the Ethereum transaction is finalized with `TransactionStatus::Success`, compute `unspent_fee = max_transaction_fee - effective_transaction_fee` and schedule a ckETH reimbursement mint to the user's `cketh_account` for that amount (minus the ckETH ledger transfer fee). This mirrors the existing behavior for ckETH withdrawals, where the unused fee portion is already reimbursed on failure.

---

### Proof of Concept

1. User calls `withdraw_erc20` with `max_transaction_fee = 3_250_000_000_000_000` wei (ckETH burned).
2. Ethereum transaction is mined with `effective_gas_price = 10 gwei`, `gas_used = 45_000` → `actual_fee = 450_000_000_000_000` wei.
3. `unspent_fee = 3_250_000_000_000_000 - 450_000_000_000_000 = 2_800_000_000_000_000` wei ≈ 0.0028 ETH lost per withdrawal.
4. `record_finalized_transaction` enters the `CkErc20` branch, checks `receipt.status == Failure` (false), and exits without creating any reimbursement request.
5. The `unspent_tx_fee` is credited to `total_unspent_tx_fees` in the minter's internal balance but is never minted back to the user. [3](#0-2) [7](#0-6)

### Citations

**File:** rs/ethereum/cketh/minter/src/main.rs (L448-458)
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
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L733-747)
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
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1147-1168)
```rust
        WithdrawalRequest::CkErc20(request) => {
            // The transaction fee is already paid and must be at most
            // the `max_transaction_fee` in the withdrawal request, which, given a gas limit, gives us an upper bound on
            // the `max_fee_per_gas`. We allocate the maximum from the beginning to minimize
            // transaction resubmissions: even if the `base_fee_per_gas` increases considerably,
            // the transaction could still make it as long as `transaction.max_fee_per_gas >=  block.base_fee_per_gas`,
            // since the `priority_fee_per_gas` received by the miner is capped to (see https://eips.ethereum.org/EIPS/eip-1559)
            // min(transaction.max_priority_fee_per_gas, transaction.max_fee_per_gas - block.base_fee_per_gas).
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

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L275-275)
```text
. The minter retrieves the receipt of the finalized transaction (as done currently by the ckETH minter) and will reimburse the ckERC20 tokens in case the transaction failed. Overcharged transaction fees are not reimbursed.
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/tests.rs (L1575-1603)
```rust
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
