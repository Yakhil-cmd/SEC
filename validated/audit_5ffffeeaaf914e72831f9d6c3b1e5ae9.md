Audit Report

## Title
Panic in `burn_from` After ckETH Burn Causes Permanent Fund Loss Without Reimbursement in `withdraw_erc20` - (File: `rs/ethereum/cketh/minter/src/ledger_client.rs`)

## Summary

The `burn_from` helper in `ledger_client.rs` unconditionally `panic!`s on `TransferFromError::BadFee`, `TooOld`, `CreatedInFuture`, and `Duplicate` variants. In the `withdraw_erc20` two-step burn flow, if any of these variants is returned by the ckERC20 ledger during the second burn (after the ckETH burn has already been finalized on the external ledger), the minter's callback traps. The IC rolls back all in-callback state changes, meaning the `EventType::FailedErc20WithdrawalRequest` reimbursement event is never recorded, and the user's ckETH is permanently destroyed with no recovery path.

## Finding Description

**Root cause**: `burn_from` in `ledger_client.rs` lines 100–130 contains four `panic!` branches:

```rust
TransferFromError::BadFee { expected_fee } => {
    panic!("BUG: bad fee, expected fee: {expected_fee}")
}
// ...
TransferFromError::TooOld => panic!("BUG: transfer too old"),
TransferFromError::CreatedInFuture { ledger_time } => {
    panic!("BUG: created in future, ledger time: {ledger_time}")
}
TransferFromError::Duplicate { duplicate_of } => {
    panic!("BUG: duplicate transfer of: {duplicate_of}")
}
``` [1](#0-0) 

**Exploit flow**:

1. `withdraw_erc20` in `main.rs` calls `cketh_ledger.burn_from(...)` (Step 1, lines 448–459). This is an inter-canister call to the ckETH ledger — once it returns `Ok`, the burn is finalized and irreversible on the external ledger. [2](#0-1) 

2. On success, execution enters the `Ok(cketh_ledger_burn_index)` branch and calls `LedgerClient::ckerc20_ledger(&ckerc20_token).burn_from(...)` (Step 2, lines 468–477). [3](#0-2) 

3. If the ckERC20 ledger returns `BadFee`, `TooOld`, `CreatedInFuture`, or `Duplicate`, `burn_from` panics inside the reply callback. The IC traps the callback and rolls back all state mutations made within it.

4. The `mutate_state` call recording `EventType::FailedErc20WithdrawalRequest` at lines 525–530 is never reached. [4](#0-3) 

5. `process_reimbursement` only iterates over entries in the event log (`reimbursement_requests_iter()`). With no entry recorded, it never mints back the ckETH.

**Why existing checks fail**: The minter passes `fee: None` and `created_at_time: None` to `transfer_from`, which under a correct ICRC-2 implementation prevents these variants. However, the minter interacts with multiple ckERC20 ledgers (added via governance), and a ledger upgrade regression or a non-conforming ledger implementation can return these variants. There is no `scopeguard` protecting the ckETH burn — unlike `withdraw.rs` lines 69–72 which uses `scopeguard` to handle exactly this scenario for reimbursements. [5](#0-4) 

## Impact Explanation

This is a **chain-fusion burn/reimbursement conservation bug** in the ckETH minter, which is explicitly in scope. A user's ckETH gas-fee deposit is permanently destroyed: the ckETH ledger records a burn with no corresponding Ethereum transaction and no mint-back. The minter's internal state never records the failed withdrawal, so no automated recovery is possible. This matches the allowed High impact: "Significant Chain Fusion, ck-token, ledger security impact with concrete user or protocol harm." The impact is bounded per-user (one `erc20_tx_fee` per failed call), placing this at **High** rather than Critical.

## Likelihood Explanation

The trigger requires a ckERC20 ledger returning one of the four panicking `TransferFromError` variants. This is constrained — ckERC20 ledgers are NNS-governed — but realistic in two scenarios that do not require a malicious actor:

1. A legitimate ckERC20 ledger upgrade introduces a regression in fee handling, causing `BadFee` to be returned.
2. A governance-approved ckERC20 token uses a non-standard ICRC-2 implementation that returns `Duplicate` unconditionally (the ICRC-2 spec only requires `Duplicate` when `created_at_time` is set, but does not prohibit returning it otherwise).

Neither scenario requires an unprivileged user to control the ledger directly; a ledger regression is sufficient. The bug is latent and will silently destroy user funds if triggered.

## Recommendation

Replace all four `panic!` branches in `burn_from` with recoverable `LedgerBurnError::TemporarilyUnavailable` variants. This ensures unexpected ledger responses propagate as `Err(LedgerBurnError::TemporarilyUnavailable)` to `withdraw_erc20`, which already handles this variant by recording `EventType::FailedErc20WithdrawalRequest` and scheduling a full reimbursement (line 508: `LedgerBurnError::TemporarilyUnavailable { .. } => erc20_tx_fee`). Alternatively, adopt the `scopeguard` pattern already used in `withdraw.rs` to guarantee the reimbursement event is recorded even if a panic occurs after the ckETH burn completes. [1](#0-0) 

## Proof of Concept

1. Deploy a mock ckERC20 ledger whose `icrc2_transfer_from` always returns `TransferFromError::BadFee { expected_fee: 1 }`.
2. Add this token to the ckETH minter (or use a PocketIC integration test that injects this response).
3. Call `withdraw_erc20` with sufficient ckETH allowance and a valid ckERC20 amount.
4. Observe: Step 1 (ckETH burn) succeeds and is recorded on the ckETH ledger at block index `N`.
5. Observe: Step 2 triggers `panic!("BUG: bad fee, expected fee: 1")` in `burn_from` at line 101.
6. Observe: The minter's event log contains no `FailedErc20WithdrawalRequest` for burn index `N`.
7. Observe: `process_reimbursement` finds no entry and never mints back the ckETH.
8. Confirm: The user's ckETH balance is permanently reduced by `erc20_tx_fee` with no Ethereum transaction sent and no reimbursement issued.

### Citations

**File:** rs/ethereum/cketh/minter/src/ledger_client.rs (L100-130)
```rust
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

**File:** rs/ethereum/cketh/minter/src/main.rs (L460-477)
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
