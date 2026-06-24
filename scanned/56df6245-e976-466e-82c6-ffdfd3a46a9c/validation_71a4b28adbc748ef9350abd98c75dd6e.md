### Title
ckETH Permanently Burned Without Reimbursement When `erc20_tx_fee ≤ CKETH_LEDGER_TRANSACTION_FEE` and ckERC20 Burn Fails - (`File: rs/ethereum/cketh/minter/src/main.rs`)

---

### Summary

In the `withdraw_erc20` function of the ckETH minter, when the ckETH gas-fee burn succeeds but the subsequent ckERC20 burn fails due to user error (`InsufficientFunds`, `AmountTooLow`, or `InsufficientAllowance`), the code silently skips creating a reimbursement request whenever `erc20_tx_fee ≤ CKETH_LEDGER_TRANSACTION_FEE`. The user's ckETH is permanently destroyed with no reimbursement, violating chain-fusion token conservation.

---

### Finding Description

The `withdraw_erc20` function performs two sequential ledger burns:

1. Burns `erc20_tx_fee` ckETH from the user's account to pay for the Ethereum gas fee.
2. Burns the requested ckERC20 amount from the user's account.

If step 2 fails with a user-attributable error, the code computes a penalized reimbursement:

```rust
// rs/ethereum/cketh/minter/src/main.rs lines 507-531
let reimbursed_amount = match &ckerc20_burn_error {
    LedgerBurnError::TemporarilyUnavailable { .. } => erc20_tx_fee,
    LedgerBurnError::InsufficientFunds { .. }
    | LedgerBurnError::AmountTooLow { .. }
    | LedgerBurnError::InsufficientAllowance { .. } => erc20_tx_fee
        .checked_sub(CKETH_LEDGER_TRANSACTION_FEE)
        .unwrap_or(Wei::ZERO),
};
if reimbursed_amount > Wei::ZERO {
    // create reimbursement request
}
```

When `erc20_tx_fee ≤ CKETH_LEDGER_TRANSACTION_FEE`, `checked_sub` returns `None`, which is mapped to `Wei::ZERO`. The guard `if reimbursed_amount > Wei::ZERO` is then false, so **no `FailedErc20WithdrawalRequest` event is emitted and no reimbursement request is ever created**. The ckETH that was already burned in step 1 is permanently destroyed. [1](#0-0) 

There is no pre-flight check anywhere in `withdraw_erc20` that enforces `erc20_tx_fee > CKETH_LEDGER_TRANSACTION_FEE` before executing the first burn. [2](#0-1) 

---

### Impact Explanation

**Vulnerability class:** Chain-fusion mint/burn/replay bug (ledger conservation bug).

When triggered, the user's ckETH tokens are burned on the ICRC-1 ledger with no corresponding ETH sent on Ethereum and no reimbursement minted back. This is an unconditional, permanent loss of user funds. The ckETH total supply decreases without any backing ETH being released, breaking the 1:1 peg invariant of the chain-fusion bridge.

The `process_reimbursement` function in `rs/ethereum/cketh/minter/src/withdraw.rs` only processes entries in `reimbursement_requests`; since no entry is ever inserted for this case, the loss is irrecoverable without a canister upgrade. [3](#0-2) 

---

### Likelihood Explanation

The trigger condition is `erc20_tx_fee ≤ CKETH_LEDGER_TRANSACTION_FEE`. The `erc20_tx_fee` is derived from a live Ethereum gas price estimate (`estimate_erc20_transaction_fee`). During periods of extremely low Ethereum base fees (which have historically occurred), the estimated fee for a 65,000-gas ERC-20 transaction can fall below the ckETH ledger's fixed transaction fee constant. The ckERC20 burn then fails if the user has insufficient allowance or balance — a common user mistake. Any unprivileged user who calls `withdraw_erc20` during such a low-gas window with an insufficient ckERC20 allowance/balance triggers the bug. No special access is required. [4](#0-3) 

---

### Recommendation

Add a pre-flight guard before executing the first ckETH burn to ensure the estimated fee exceeds the ledger transaction fee, so a reimbursement is always possible:

```rust
if erc20_tx_fee <= CKETH_LEDGER_TRANSACTION_FEE {
    return Err(WithdrawErc20Error::TemporarilyUnavailable(
        "Estimated gas fee too low to cover ledger transaction fee".to_string()
    ));
}
```

Alternatively, when `reimbursed_amount` is zero, still create a reimbursement request for the full `erc20_tx_fee` (absorbing the ledger fee as a protocol cost) rather than silently dropping it. The `TemporarilyUnavailable` branch already does this correctly — the same treatment should apply to user-error branches. [5](#0-4) 

---

### Proof of Concept

1. Ethereum gas prices drop to an extremely low level such that `estimate_erc20_transaction_fee()` returns a value `F` where `F ≤ CKETH_LEDGER_TRANSACTION_FEE`.
2. An unprivileged user calls `withdraw_erc20` with a valid `ckerc20_ledger_id`, a `recipient` address, and an `amount` they do not actually hold (or have not approved the minter for on the ckERC20 ledger).
3. The minter calls `cketh_ledger.burn_from(cketh_account, F, ...)`. This succeeds, recording `cketh_ledger_burn_index`. The user's ckETH balance decreases by `F`.
4. The minter calls `ckerc20_ledger.burn_from(ckerc20_account, amount, ...)`. This fails with `LedgerBurnError::InsufficientAllowance` or `InsufficientFunds`.
5. The code computes `reimbursed_amount = F.checked_sub(CKETH_LEDGER_TRANSACTION_FEE).unwrap_or(Wei::ZERO) = Wei::ZERO`.
6. The condition `if reimbursed_amount > Wei::ZERO` is false. No `FailedErc20WithdrawalRequest` event is emitted.
7. The function returns `Err(WithdrawErc20Error::CkErc20LedgerError { ... })` to the caller.
8. The user's `F` ckETH is permanently burned. No reimbursement is ever processed. [6](#0-5) [7](#0-6)

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

**File:** rs/ethereum/cketh/minter/src/main.rs (L506-536)
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
                    Err(WithdrawErc20Error::CkErc20LedgerError {
                        cketh_block_index: Nat::from(cketh_ledger_burn_index.get()),
                        error: ckerc20_burn_error.into(),
                    })
                }
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L46-63)
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
```
