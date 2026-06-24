### Title
Panic in `burn_from` Callback After ckETH Burn Leaves User Funds Permanently Lost Without Reimbursement in `withdraw_erc20` Multi-Canister Flow - (File: `rs/ethereum/cketh/minter/src/ledger_client.rs`)

---

### Summary

The `withdraw_erc20` flow in the ckETH minter performs two sequential inter-canister burns: first ckETH (gas fee), then ckERC20 (withdrawal amount). The `burn_from` helper in `ledger_client.rs` unconditionally `panic!`s on several `TransferFromError` variants it considers "unreachable" (`BadFee`, `TooOld`, `CreatedInFuture`, `Duplicate`). If any of these variants is returned by the ckERC20 ledger during the **second** burn, the minter's callback traps. Because the ckETH burn on the ckETH ledger is already finalized (external state), and the minter's `FailedErc20WithdrawalRequest` reimbursement event is never recorded (the callback state is rolled back), the user's ckETH is permanently destroyed with no reimbursement path.

---

### Finding Description

The `withdraw_erc20` function in `rs/ethereum/cketh/minter/src/main.rs` executes a two-step multi-canister burn flow:

**Step 1** — burn ckETH for gas fee: [1](#0-0) 

**Step 2** — burn ckERC20 for the withdrawal amount (only reached after Step 1 succeeds): [2](#0-1) 

The `burn_from` helper that executes both burns contains explicit `panic!` calls for several `TransferFromError` variants: [3](#0-2) 

These panics are labeled `"BUG: ..."` and treated as unreachable. However, if the ckERC20 ledger returns `BadFee`, `TooOld`, `CreatedInFuture`, or `Duplicate` during Step 2, `burn_from` panics inside the callback. On the IC, a trap in a callback rolls back all state changes made **within that callback**, but does **not** undo external inter-canister calls already completed.

The consequence:
- The ckETH burn (Step 1) is finalized on the ckETH ledger — it is an external, irreversible state change.
- The minter's callback traps before reaching the `mutate_state` call that records `EventType::FailedErc20WithdrawalRequest` (lines 525–530), which is the only mechanism that schedules a reimbursement.
- No reimbursement is ever queued. The user's ckETH is permanently burned.

The reimbursement scheduling code that is bypassed: [4](#0-3) 

The `process_reimbursement` function that would have minted back the ckETH only processes entries recorded in the event log: [5](#0-4) 

---

### Impact Explanation

A user calling `withdraw_erc20` loses their ckETH gas-fee deposit permanently. The ckETH ledger records a burn with no corresponding mint-back. The minter's internal state never records the failed withdrawal, so the reimbursement timer task (`process_reimbursement`) has no entry to process. The user has no recovery path without manual governance intervention. This is a **chain-fusion burn/reimbursement conservation bug**: ckETH supply decreases without a corresponding Ethereum transaction or reimbursement.

---

### Likelihood Explanation

The four panicking variants (`BadFee`, `TooOld`, `CreatedInFuture`, `Duplicate`) are suppressed by the minter passing `fee: None` and `created_at_time: None` to `transfer_from`. Under a correct ICRC-2 implementation these variants should not be returned. However:

1. A ckERC20 ledger upgrade that introduces a regression in fee handling could return `BadFee`.
2. A ckERC20 ledger with a non-standard or buggy ICRC-2 implementation (e.g., a community-deployed token added via governance) could return any of these variants.
3. The `Duplicate` variant is documented as only triggered when `created_at_time` is set, but a non-conforming ledger could return it unconditionally.

The minter's own comment in `burn_from` acknowledges the risk of unexpected panics in callbacks in the analogous `update_balance` flow: [6](#0-5) 

The ckBTC minter uses a `scopeguard` to handle exactly this scenario. The ckETH minter's `withdraw_erc20` has no equivalent guard for the ckETH burn.

---

### Recommendation

Replace all `panic!("BUG: ...")` branches in `burn_from` with recoverable `LedgerBurnError` variants (e.g., `TemporarilyUnavailable`). This ensures that even if the ckERC20 ledger returns an unexpected error, the error propagates as `Err(LedgerBurnError::...)` to `withdraw_erc20`, which then records `EventType::FailedErc20WithdrawalRequest` and schedules a reimbursement. Alternatively, adopt the `scopeguard` pattern used in `update_balance.rs` to guarantee the reimbursement event is recorded even if a panic occurs after the ckETH burn.

---

### Proof of Concept

1. User calls `withdraw_erc20` with a valid ckERC20 token and sufficient ckETH allowance.
2. Step 1 succeeds: ckETH is burned on the ckETH ledger (block index `N` recorded).
3. The ckERC20 ledger (buggy or upgraded) returns `TransferFromError::BadFee { expected_fee: X }` for the Step 2 `transfer_from` call.
4. `burn_from` executes `panic!("BUG: bad fee, expected fee: {X}")` at line 101 of `ledger_client.rs`.
5. The minter's reply callback traps. IC execution rolls back all in-callback state changes.
6. No `EventType::FailedErc20WithdrawalRequest` is written to the event log.
7. `process_reimbursement` finds no entry for burn index `N` and never mints back the ckETH.
8. The user's ckETH balance is permanently reduced by `erc20_tx_fee` with no Ethereum transaction sent and no reimbursement issued. [7](#0-6) [8](#0-7)

### Citations

**File:** rs/ethereum/cketh/minter/src/main.rs (L448-459)
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
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L460-504)
```rust
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

**File:** rs/ethereum/cketh/minter/src/ledger_client.rs (L99-130)
```rust
                let burn_error = match transfer_from_error {
                    TransferFromError::BadFee { expected_fee } => {
                        panic!("BUG: bad fee, expected fee: {expected_fee}")
                    }
                    TransferFromError::BadBurn { min_burn_amount } => {
                        LedgerBurnError::AmountTooLow {
                            minimum_burn_amount: min_burn_amount,
                            failed_burn_amount: amount.clone(),
                            ledger: self.ck_ledger(),
                        }
                    }
                    TransferFromError::InsufficientFunds { balance } => {
                        LedgerBurnError::InsufficientFunds {
                            balance,
                            failed_burn_amount: amount.clone(),
                            ledger: self.ck_ledger(),
                        }
                    }
                    TransferFromError::InsufficientAllowance { allowance } => {
                        LedgerBurnError::InsufficientAllowance {
                            allowance,
                            failed_burn_amount: amount,
                            ledger: self.ck_ledger(),
                        }
                    }
                    TransferFromError::TooOld => panic!("BUG: transfer too old"),
                    TransferFromError::CreatedInFuture { ledger_time } => {
                        panic!("BUG: created in future, ledger time: {ledger_time}")
                    }
                    TransferFromError::Duplicate { duplicate_of } => {
                        panic!("BUG: duplicate transfer of: {duplicate_of}")
                    }
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L55-72)
```rust
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
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L327-337)
```rust
        // After the call to `mint_ckbtc` returns, in a very unlikely situation the
        // execution may panic/trap without persisting state changes and then we will
        // have no idea whether the mint actually succeeded or not. If this happens
        // the use of the guard below will help set the utxo to `CleanButMintUnknown`
        // status so that it will not be minted again. Utxos with this status will
        // require manual intervention.
        let guard = scopeguard::guard((utxo.clone(), caller_account), |(utxo, account)| {
            mutate_state(|s| {
                state::audit::mark_utxo_checked_mint_unknown(s, utxo, account, runtime)
            });
        });
```
