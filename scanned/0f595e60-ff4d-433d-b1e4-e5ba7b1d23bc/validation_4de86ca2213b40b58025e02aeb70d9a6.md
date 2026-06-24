### Title
Silent ckETH Fund Loss When `erc20_tx_fee <= CKETH_LEDGER_TRANSACTION_FEE` During Failed ckERC20 Withdrawal - (File: `rs/ethereum/cketh/minter/src/main.rs`)

---

### Summary

In the `withdraw_erc20` function of the ckETH minter, when the ckETH gas-fee burn succeeds but the subsequent ckERC20 token burn fails, the minter attempts to reimburse the user's ckETH. However, the reimbursement amount is computed as `erc20_tx_fee - CKETH_LEDGER_TRANSACTION_FEE`, and if `erc20_tx_fee <= CKETH_LEDGER_TRANSACTION_FEE` (i.e., the saturating subtraction yields `Wei::ZERO`), **no reimbursement request is ever created**. The user's ckETH is permanently burned with no recovery path.

---

### Finding Description

The `withdraw_erc20` endpoint performs two sequential ledger burns:

1. **First burn**: `erc20_tx_fee` worth of ckETH is burned from the user's ckETH account to pay for the Ethereum gas fee.
2. **Second burn**: `ckerc20_withdrawal_amount` of ckERC20 tokens is burned from the user's ckERC20 account.

This is structurally analogous to the Aave bug: a two-step deduction where the first step succeeds and the second step can fail, leaving the user's funds from the first step unrecovered.

When the second burn (ckERC20) fails with `InsufficientFunds`, `AmountTooLow`, or `InsufficientAllowance`, the reimbursement amount is computed as:

```rust
erc20_tx_fee
    .checked_sub(CKETH_LEDGER_TRANSACTION_FEE)
    .unwrap_or(Wei::ZERO)
```

where `CKETH_LEDGER_TRANSACTION_FEE = Wei::new(2_000_000_000_000_u128)` (2,000 Gwei).

The reimbursement is only scheduled **if** `reimbursed_amount > Wei::ZERO`:

```rust
if reimbursed_amount > Wei::ZERO {
    // schedule reimbursement
}
```

If `erc20_tx_fee <= CKETH_LEDGER_TRANSACTION_FEE`, `reimbursed_amount` becomes `Wei::ZERO`, the `if` branch is skipped, and **no `FailedErc20WithdrawalRequest` event is emitted and no reimbursement is ever scheduled**. The ckETH burned in step 1 is permanently lost.

The `erc20_tx_fee` is estimated dynamically from current Ethereum gas prices. During periods of extremely low gas prices (e.g., near-zero base fee), `erc20_tx_fee` can be at or below 2,000 Gwei. The minimum withdrawal amount check is enforced only for ckETH withdrawals, not for the ckETH gas-fee component of ckERC20 withdrawals. [1](#0-0) [2](#0-1) 

---

### Impact Explanation

A user who calls `withdraw_erc20` during a period of very low Ethereum gas prices (such that `erc20_tx_fee <= 2_000_000_000_000 wei`) and whose ckERC20 burn subsequently fails (e.g., due to insufficient ckERC20 balance or allowance) will have their ckETH gas-fee burn permanently unrecovered. The ckETH is burned on the ledger with no corresponding reimbursement mint. This is a **ledger conservation bug**: ckETH tokens are destroyed without the user receiving any value (no ETH was sent on Ethereum, no ckERC20 was burned).

The impact is permanent, irreversible loss of user ckETH funds. The magnitude per incident equals `erc20_tx_fee` (up to 2,000 Gwei worth of ckETH), which at current ETH prices is small per event but can accumulate across many users. [3](#0-2) [4](#0-3) 

---

### Likelihood Explanation

The trigger requires two concurrent conditions:
1. Ethereum gas prices are extremely low such that `erc20_tx_fee <= 2_000_000_000_000 wei` (2,000 Gwei total fee for 65,000 gas = ~0.031 Gwei per gas). This is rare on mainnet but has occurred historically and is more common on testnets.
2. The ckERC20 burn fails (user has insufficient ckERC20 balance/allowance, or amount is too small).

An unprivileged user can trigger this by calling `withdraw_erc20` with a valid ckETH approval but an insufficient ckERC20 approval or balance, during a low-gas-price window. No special privileges are required. The attacker-controlled entry path is the public `withdraw_erc20` update endpoint. [5](#0-4) [6](#0-5) 

---

### Recommendation

Replace the silent `unwrap_or(Wei::ZERO)` with unconditional reimbursement of the full `erc20_tx_fee` when the ckERC20 burn fails due to user error, or at minimum always schedule a reimbursement request even when `reimbursed_amount == Wei::ZERO` (skipping the `> Wei::ZERO` guard). The penalty deduction of `CKETH_LEDGER_TRANSACTION_FEE` should only be applied when the resulting reimbursement is still positive; if the fee is too small to cover the penalty, the full amount should be returned rather than silently forfeited.

Concretely, change:

```rust
if reimbursed_amount > Wei::ZERO {
    // schedule reimbursement
}
```

to always schedule a reimbursement for the full `erc20_tx_fee` when the ckERC20 burn fails, regardless of whether the penalty subtraction underflows. [7](#0-6) 

---

### Proof of Concept

1. Ethereum gas prices drop to near-zero (e.g., `base_fee_per_gas = 0`, `max_priority_fee_per_gas = 0`), causing `erc20_tx_fee` to be estimated at, say, `1_000_000_000_000 wei` (1,000 Gwei), which is less than `CKETH_LEDGER_TRANSACTION_FEE = 2_000_000_000_000 wei`.

2. User approves the minter to burn `1_000_000_000_000 wei` of ckETH (the estimated fee) but provides **no** ckERC20 allowance.

3. User calls `withdraw_erc20(amount=X, ckerc20_ledger_id=..., recipient=...)`.

4. The minter successfully burns `1_000_000_000_000 wei` of ckETH from the user's account (step 1 succeeds).

5. The minter attempts to burn `X` ckERC20 tokens; this fails with `InsufficientAllowance`.

6. The code computes:
   ```
   reimbursed_amount = 1_000_000_000_000.checked_sub(2_000_000_000_000).unwrap_or(Wei::ZERO)
                     = Wei::ZERO
   ```

7. The `if reimbursed_amount > Wei::ZERO` guard is false; no `FailedErc20WithdrawalRequest` event is emitted, no reimbursement is scheduled.

8. The user's `1_000_000_000_000 wei` of ckETH is permanently burned. No ETH was sent on Ethereum. No ckERC20 was burned. The user receives nothing. [8](#0-7) [2](#0-1) [1](#0-0)

### Citations

**File:** rs/ethereum/cketh/minter/src/main.rs (L59-59)
```rust
pub const CKETH_LEDGER_TRANSACTION_FEE: Wei = Wei::new(2_000_000_000_000_u128);
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L389-398)
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
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L430-432)
```rust
    let erc20_tx_fee = estimate_erc20_transaction_fee().await.ok_or_else(|| {
        WithdrawErc20Error::TemporarilyUnavailable("Failed to retrieve current gas fee".to_string())
    })?;
```

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
