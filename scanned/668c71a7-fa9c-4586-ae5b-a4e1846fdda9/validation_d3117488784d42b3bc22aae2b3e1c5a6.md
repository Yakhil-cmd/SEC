### Title
ckETH Permanently Lost When `erc20_tx_fee ≤ CKETH_LEDGER_TRANSACTION_FEE` and ckERC20 Burn Fails - (File: rs/ethereum/cketh/minter/src/main.rs)

### Summary
In `withdraw_erc20`, the minter performs a two-step burn: first ckETH (for gas fees), then ckERC20 (the actual token). If the ckERC20 burn fails with a user-attributable error, the code computes a `reimbursed_amount` by subtracting `CKETH_LEDGER_TRANSACTION_FEE` from `erc20_tx_fee`. When `erc20_tx_fee ≤ CKETH_LEDGER_TRANSACTION_FEE`, the result is `Wei::ZERO`, and the guarding condition `if reimbursed_amount > Wei::ZERO` silently skips scheduling any reimbursement. The already-burned ckETH is permanently lost with no recovery path — a direct analog to the Morpho "hard withdraw" stuck-position bug.

### Finding Description
`withdraw_erc20` in `rs/ethereum/cketh/minter/src/main.rs` executes two sequential inter-canister calls:

1. **Step 1 (lines 448–459):** Burns `erc20_tx_fee` ckETH from the user's account via `cketh_ledger.burn_from(...)`. On success, `cketh_ledger_burn_index` is obtained.
2. **Step 2 (lines 468–477):** Burns `ckerc20_withdrawal_amount` ckERC20 from the user's account via `ckerc20_ledger.burn_from(...)`.

If Step 2 fails with `InsufficientFunds`, `AmountTooLow`, or `InsufficientAllowance`, the error branch at lines 506–536 computes:

```rust
let reimbursed_amount = match &ckerc20_burn_error {
    LedgerBurnError::TemporarilyUnavailable { .. } => erc20_tx_fee,
    LedgerBurnError::InsufficientFunds { .. }
    | LedgerBurnError::AmountTooLow { .. }
    | LedgerBurnError::InsufficientAllowance { .. } => erc20_tx_fee
        .checked_sub(CKETH_LEDGER_TRANSACTION_FEE)
        .unwrap_or(Wei::ZERO),   // ← silently becomes ZERO
};
if reimbursed_amount > Wei::ZERO {                // ← gate skipped when ZERO
    // schedule FailedErc20WithdrawalRequest reimbursement
}
// ckETH already burned; no reimbursement event emitted
```

`CKETH_LEDGER_TRANSACTION_FEE = Wei::new(2_000_000_000_000)` (2 trillion wei, defined at line 59). When `erc20_tx_fee ≤ CKETH_LEDGER_TRANSACTION_FEE`, `checked_sub` returns `None`, `unwrap_or` yields `Wei::ZERO`, and the `if` guard suppresses the `EventType::FailedErc20WithdrawalRequest` event entirely. No reimbursement request is ever enqueued in `eth_transactions.reimbursement_requests`, so `process_reimbursement` never sees it. The ckETH burned in Step 1 is gone permanently.

This is structurally identical to the Morpho bug: a multi-step operation where Step 1 succeeds (ckETH burned / P2P supplier funds moved), Step 2 fails (ckERC20 burn / pool borrow), and the system enters a boundary state from which neither forward progress nor backward recovery is possible.

### Impact Explanation
**Ledger conservation bug / chain-fusion mint-burn-replay bug.** ckETH tokens are burned on the ckETH ledger but never re-minted as reimbursement. The total ckETH supply is permanently reduced by `erc20_tx_fee` without any corresponding Ethereum-side event. The user loses their entire gas-fee deposit with no on-chain record of the loss (no `FailedErc20WithdrawalRequest` event is emitted), making the loss invisible to auditors and dashboards.

### Likelihood Explanation
**Low.** The boundary condition requires `erc20_tx_fee ≤ 2_000_000_000_000 wei`. With `CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT = 65_000` gas, this implies an effective gas price below ~0.03 gwei — far below typical Ethereum mainnet levels (1–100+ gwei). However:
- The condition is reachable on Ethereum testnets or during extreme low-activity periods.
- The user-attributable error branch (`InsufficientFunds` / `InsufficientAllowance`) is easily triggered by any unprivileged caller who approves ckETH but not ckERC20 (or approves insufficient ckERC20).
- No privileged access is required; any `withdraw_erc20` caller can reach this path.

### Recommendation
Remove the `if reimbursed_amount > Wei::ZERO` guard, or replace it with an assertion that `erc20_tx_fee > CKETH_LEDGER_TRANSACTION_FEE` before accepting the withdrawal. At minimum, always emit a `FailedErc20WithdrawalRequest` event (even with `reimbursed_amount = 0`) so the loss is auditable. A stronger fix is to reject the `withdraw_erc20` call upfront if the estimated fee does not exceed `CKETH_LEDGER_TRANSACTION_FEE`, preventing the ckETH burn from occurring at all.

### Proof of Concept
1. Ethereum gas prices are at an extreme low (< 0.03 gwei — possible on testnets or edge mainnet conditions).
2. Unprivileged user calls `withdraw_erc20` with a valid ckETH `icrc2_approve` for `erc20_tx_fee` but **no** ckERC20 approval (or insufficient ckERC20 balance).
3. **Step 1:** `cketh_ledger.burn_from(cketh_account, erc20_tx_fee, ...)` succeeds → `cketh_ledger_burn_index` obtained. [1](#0-0) 
4. **Step 2:** `ckerc20_ledger.burn_from(...)` returns `Err(LedgerBurnError::InsufficientAllowance { .. })`. [2](#0-1) 
5. `reimbursed_amount = erc20_tx_fee.checked_sub(CKETH_LEDGER_TRANSACTION_FEE).unwrap_or(Wei::ZERO)` → `Wei::ZERO` because `erc20_tx_fee ≤ 2_000_000_000_000`. [3](#0-2) 
6. `if reimbursed_amount > Wei::ZERO` is `false` → no `EventType::FailedErc20WithdrawalRequest` is emitted, no reimbursement request is enqueued. [4](#0-3) 
7. `process_reimbursement` timer never sees this burn index; ckETH is permanently destroyed. [5](#0-4) 
8. The `CKETH_LEDGER_TRANSACTION_FEE` constant that creates the boundary: [6](#0-5)

### Citations

**File:** rs/ethereum/cketh/minter/src/main.rs (L59-59)
```rust
pub const CKETH_LEDGER_TRANSACTION_FEE: Wei = Wei::new(2_000_000_000_000_u128);
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

**File:** rs/ethereum/cketh/minter/src/main.rs (L468-477)
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
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L509-513)
```rust
                        LedgerBurnError::InsufficientFunds { .. }
                        | LedgerBurnError::AmountTooLow { .. }
                        | LedgerBurnError::InsufficientAllowance { .. } => erc20_tx_fee
                            .checked_sub(CKETH_LEDGER_TRANSACTION_FEE)
                            .unwrap_or(Wei::ZERO),
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L515-531)
```rust
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

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L55-60)
```rust
    let reimbursements: Vec<(ReimbursementIndex, ReimbursementRequest)> = read_state(|s| {
        s.eth_transactions
            .reimbursement_requests_iter()
            .map(|(index, request)| (index.clone(), request.clone()))
            .collect()
    });
```
