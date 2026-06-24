### Title
Stale Gas Fee Estimate in `withdraw_erc20` Causes ckETH Ledger Conservation Bug When Ethereum Gas Fees Spike - (File: rs/ethereum/cketh/minter/src/main.rs)

---

### Summary

The `withdraw_erc20` function in the ckETH minter burns ckETH for a gas fee derived from a potentially stale estimate (cached up to 60 seconds). The burned `max_transaction_fee` is locked into the withdrawal request. If Ethereum gas fees spike beyond the 2× buffer between the estimate and when the asynchronous transaction creation occurs (potentially minutes later), `create_transaction` returns `InsufficientTransactionFee`. The ckETH is already burned with no corresponding ETH transaction sent, breaking the ckETH conservation invariant.

---

### Finding Description

In `withdraw_erc20`, the sequence is:

**Step 1 — Estimate fee (potentially stale):**
`estimate_erc20_transaction_fee()` calls `lazy_refresh_gas_fee_estimate()`, which serves a cached estimate if it is less than `MAX_AGE_NS = 60_000_000_000` ns (60 seconds) old. [1](#0-0) 

The estimate formula is `max_fee_per_gas = 2 × base_fee_per_gas + max_priority_fee_per_gas`. [2](#0-1) 

**Step 2 — Burn ckETH for the estimated fee:**
The stale `erc20_tx_fee` is used immediately to burn ckETH from the user's account. [3](#0-2) 

**Step 3 — Burn ckERC20 and queue withdrawal request:**
The `max_transaction_fee` field in the queued request is permanently set to the already-burned `erc20_tx_fee`. [4](#0-3) 

**Step 4 — Asynchronous transaction creation check:**
When the withdrawal is later processed, `create_transaction` computes `actual_min_max_fee_per_gas = base_fee_at_tx_time + max_priority_at_tx_time` and compares it against `request_max_fee_per_gas` derived from the burned amount. If gas fees have spiked, the check fails: [5](#0-4) 

For the error to trigger, the base fee must exceed `2 × base_fee_at_estimate_time`, which is realistic during Ethereum gas spikes. The window is not just 60 seconds — the withdrawal request is processed asynchronously by a timer, potentially minutes after the burn.

---

### Impact Explanation

When `InsufficientTransactionFee` is returned for a ckERC20 withdrawal:

- The ckETH burned for gas fee is already consumed on the IC ledger.
- No Ethereum transaction is sent, so the minter's on-chain ETH balance is not reduced.
- The ckETH total supply decreases without a corresponding decrease in the minter's ETH holdings, breaking the 1:1 ckETH↔ETH conservation invariant.
- The ckERC20 tokens burned for the withdrawal amount also require reimbursement, but the ckETH gas fee burn has no corresponding reimbursement path visible in the `withdraw_erc20` handler. [6](#0-5) 

The reimbursement logic shown above only covers the case where the **ckERC20 burn itself fails** — not the later `InsufficientTransactionFee` path during transaction creation. The `cketh_ledger_burn_index` is included in the error struct, suggesting the intent to reimburse, but no reimbursement event is emitted in the `create_transaction` error path.

---

### Likelihood Explanation

- Ethereum gas fees routinely spike by 3–10× during high-demand events (NFT mints, DeFi liquidations, token launches).
- The 60-second cache means a user calling `withdraw_erc20` at second 59 of the cache window burns ckETH based on a nearly 60-second-old estimate.
- The withdrawal request is then queued and processed asynchronously — the timer-based processing can add additional minutes of delay before `create_transaction` is called.
- Any unprivileged user can trigger this by calling `withdraw_erc20` during a gas fee spike window; no special access is required. [7](#0-6) 

---

### Recommendation

1. **Refresh the gas fee estimate immediately before the ckETH burn** rather than relying on a cached value, eliminating the stale-estimate window.
2. **Add an explicit reimbursement event** for the ckETH burned for gas fee when `InsufficientTransactionFee` is returned during `create_transaction` for ckERC20 withdrawals, mirroring the existing ckERC20 reimbursement path.
3. **Emit a `FailedErc20WithdrawalRequest`-equivalent event** that covers the full burned ckETH amount (not just the ckERC20 amount) when transaction creation fails due to insufficient fee.

---

### Proof of Concept

1. Ethereum base fee is `B` at time `T`. The CMC cache holds this estimate.
2. At time `T + 55s`, user calls `withdraw_erc20`. The cached estimate (55s old, within 60s window) is used: `erc20_tx_fee = (2B + priority) × gas_limit`.
3. ckETH is burned for `erc20_tx_fee`; ckERC20 is burned for the withdrawal amount. Both burns succeed on the IC ledger.
4. At time `T + 60s`, Ethereum gas spikes: base fee becomes `3B`.
5. At time `T + 180s`, the timer processes the withdrawal and calls `create_transaction`:
   - `actual_min_max_fee_per_gas = 3B + priority`
   - `request_max_fee_per_gas = 2B + priority`
   - `3B + priority > 2B + priority` → `InsufficientTransactionFee` returned.
6. No Ethereum transaction is sent. The ckETH burned for gas fee has no reimbursement path. ckETH supply is permanently reduced without a corresponding ETH outflow — conservation invariant broken. [8](#0-7) [9](#0-8)

### Citations

**File:** rs/ethereum/cketh/minter/src/tx.rs (L517-523)
```rust
    pub fn checked_estimate_max_fee_per_gas(&self) -> Option<WeiPerGas> {
        self.base_fee_per_gas
            .checked_mul(2_u8)
            .and_then(|base_fee_estimate| {
                base_fee_estimate.checked_add(self.max_priority_fee_per_gas)
            })
    }
```

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

**File:** rs/ethereum/cketh/minter/src/main.rs (L479-492)
```rust
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
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L506-535)
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
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L545-553)
```rust
async fn estimate_erc20_transaction_fee() -> Option<Wei> {
    lazy_refresh_gas_fee_estimate()
        .await
        .map(|gas_fee_estimate| {
            gas_fee_estimate
                .to_price(CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT)
                .max_transaction_fee()
        })
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
