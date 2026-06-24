Audit Report

## Title
Stale Gas Fee Estimate in `withdraw_erc20` Causes Permanently Stuck Withdrawals with No Reimbursement Path - (File: `rs/ethereum/cketh/minter/src/main.rs`)

## Summary

`withdraw_erc20` burns both ckETH (as gas fee) and ckERC20 tokens using a gas fee estimate that may be up to 60 seconds stale. If Ethereum gas fees rise between the burn and the background task's attempt to create the transaction, `create_transaction` returns `CreateTransactionError::InsufficientTransactionFee`, and the handler in `create_transactions_batch` only reschedules the request with no reimbursement. Because the ckERC20 reimbursement path only triggers on a finalized on-chain failure receipt, a request that is never submitted to Ethereum results in permanently burned user funds with no recovery mechanism.

## Finding Description

**Root cause — stale fee used to burn tokens:**

`estimate_erc20_transaction_fee()` delegates to `lazy_refresh_gas_fee_estimate()`, which caches the estimate for up to 60 seconds (`MAX_AGE_NS = 60_000_000_000`). [1](#0-0) [2](#0-1) 

The stale `erc20_tx_fee` is used to burn ckETH from the user: [3](#0-2) 

After the ckETH burn succeeds, ckERC20 tokens are burned and the request is queued with `max_transaction_fee = erc20_tx_fee`: [4](#0-3) 

**Transaction creation fails silently when fees have risen:**

`create_transaction` computes the current minimum `max_fee_per_gas` and compares it against the burned amount. If fees have risen, it returns `InsufficientTransactionFee`: [5](#0-4) 

The handler in `create_transactions_batch` only reschedules — no reimbursement is issued: [6](#0-5) 

**No reimbursement path for this scenario:**

The ckERC20 reimbursement logic only triggers when a transaction is finalized with a failure receipt (`receipt.status == TransactionStatus::Failure`): [7](#0-6) 

The `FailedErc20WithdrawalRequest` event (which does schedule reimbursement) is only emitted in `withdraw_erc20` when the ckERC20 ledger burn itself fails — not when `create_transaction` later fails with `InsufficientTransactionFee`: [8](#0-7) 

There is no retry counter, age threshold, or timeout in the rescheduling path that would eventually trigger reimbursement for a permanently stuck request.

## Impact Explanation

Any user calling `withdraw_erc20` during a gas fee spike can have both their ckETH gas fee and their ckERC20 withdrawal amount permanently burned with no recovery. The ckERC20 tokens are destroyed on the ledger at withdrawal time; if the Ethereum transaction is never created, those tokens are gone. This constitutes a concrete, permanent loss of in-scope chain-key/ck-token assets for affected users, matching the **High** impact class: "Significant Chain Fusion, ck-token, ledger … security impact with concrete user or protocol harm."

## Likelihood Explanation

Ethereum gas fees can spike dramatically within seconds during high-demand events. The 60-second cache window in `lazy_refresh_gas_fee_estimate` creates a meaningful race window. Any unprivileged user calling `withdraw_erc20` is exposed without warning. No special privileges or attacker action are required — the condition is triggered by normal market volatility. The DFINITY team acknowledged a related fee-estimation issue in the August 2024 minter upgrade, confirming the scenario is realistic. [9](#0-8) 

## Recommendation

1. In `create_transactions_batch`, when `CreateTransactionError::InsufficientTransactionFee` is returned for a `CkErc20` request that has exceeded a configurable maximum retry count or age threshold (e.g., based on `request.created_at`), emit a `FailedErc20WithdrawalRequest` event to reimburse the burned ckETH gas fee, and schedule a `ReimbursementRequest` on the ckERC20 ledger to return the burned ckERC20 tokens.
2. Alternatively, enforce at `withdraw_erc20` call time that the fee estimate is freshly fetched (bypassing the cache) before burning any tokens, and reject the call if a fresh estimate cannot be obtained.

## Proof of Concept

1. Ethereum gas fees are at `X` wei/gas. The minter's cached estimate is `X` (fresh within 60 seconds).
2. User calls `withdraw_erc20`. The minter burns `X * 65_000` ckETH as gas fee and burns the ckERC20 withdrawal amount. The `Erc20WithdrawalRequest` is queued with `max_transaction_fee = X * 65_000`.
3. Within 60 seconds, Ethereum gas fees spike to `10X` wei/gas.
4. `create_transactions_batch` runs with the current `gas_fee_estimate` reflecting `10X`. `create_transaction` computes `actual_min_max_fee_per_gas = 10X > request_max_fee_per_gas = X`, returns `InsufficientTransactionFee`.
5. The handler calls `reschedule_withdrawal_request` — the request goes to the back of the queue. No reimbursement is scheduled.
6. Gas fees remain elevated. Steps 4–5 repeat indefinitely.
7. The user's ckETH and ckERC20 tokens are permanently burned with no reimbursement path.

A deterministic integration test can reproduce this by: (a) setting the minter's cached gas fee estimate to `X`, (b) calling `withdraw_erc20` to burn tokens, (c) updating the minter's gas fee state to `10X`, (d) invoking `create_transactions_batch`, and (e) asserting that no `ReimbursementRequest` is ever scheduled for the ckERC20 ledger burn index.

### Citations

**File:** rs/ethereum/cketh/minter/src/tx.rs (L610-611)
```rust
pub async fn lazy_refresh_gas_fee_estimate() -> Option<GasFeeEstimate> {
    const MAX_AGE_NS: u64 = 60_000_000_000_u64; //60 seconds
```

**File:** rs/ethereum/cketh/minter/src/tx.rs (L672-680)
```rust
    let now_ns = ic_cdk::api::time();
    match read_state(|s| s.last_transaction_price_estimate.clone()) {
        Some((last_estimate_timestamp_ns, estimate))
            if now_ns < last_estimate_timestamp_ns.saturating_add(MAX_AGE_NS) =>
        {
            Some(estimate)
        }
        _ => do_refresh().await,
    }
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

**File:** rs/ethereum/cketh/minter/src/main.rs (L468-504)
```rust
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

**File:** rs/ethereum/cketh/minter/src/main.rs (L506-531)
```rust
                Err(ckerc20_burn_error) => {
                    let reimbursed_amount = match &ckerc20_burn_error {
                        LedgerBurnError::TemporarilyUnavailable { .. } => erc20_tx_fee, //don't penalize user in case of an error outside of their control
                        LedgerBurnError::InsufficientFunds { .. }
                        | LedgerBurnError::AmountTooLow { .. }
                        | LedgerBurnError::InsufficientAllowance { .. } => erc20_tx_fee
                            .checked_sub(CKETH_LEDGER_TRANSACTION_FEE)
                            .unwrap_or(Wei::ZERO),
                    };
                    if reimbursed_amount > Wei::ZERO {
                        let reimbursement_request = ReimbursementRequest {
                            ledger_burn_index: cketh_ledger_burn_index,
                            reimbursed_amount: reimbursed_amount.change_units(),
                            to: cketh_account.owner,
                            to_subaccount: cketh_account
                                .subaccount
                                .and_then(LedgerSubaccount::from_bytes),
                            transaction_hash: None,
                        };
                        mutate_state(|s| {
                            process_event(
                                s,
                                EventType::FailedErc20WithdrawalRequest(reimbursement_request),
                            );
                        });
                    }
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L733-746)
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

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L281-291)
```rust
            Err(CreateTransactionError::InsufficientTransactionFee {
                cketh_ledger_burn_index: ledger_burn_index,
                allowed_max_transaction_fee: withdrawal_amount,
                actual_max_transaction_fee: max_transaction_fee,
            }) => {
                log!(
                    INFO,
                    "[create_transactions_batch]: Withdrawal request with burn index {ledger_burn_index} has insufficient amount {withdrawal_amount:?} to cover transaction fees: {max_transaction_fee:?}. Request moved back to end of queue."
                );
                mutate_state(|s| s.eth_transactions.reschedule_withdrawal_request(request));
            }
```

**File:** rs/ethereum/cketh/mainnet/minter_upgrade_2024_08_05.md (L16-17)
```markdown
* Fix a bug affecting ckERC20 withdrawals which were unnecessarily delayed as soon as the estimated transaction fees increased.
* Expand the existing method `eip_1559_transaction_price` to also return the estimated transaction price of a ckERC20 withdrawal.
```
