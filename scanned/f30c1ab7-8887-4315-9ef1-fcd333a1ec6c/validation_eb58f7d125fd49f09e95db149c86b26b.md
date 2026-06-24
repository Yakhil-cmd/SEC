### Title
ICRC-1 Ledger `icrc2_approve` Fee Always Burned Instead of Credited to Fee Collector - (`File: rs/ledger_suite/icrc1/src/lib.rs`)

---

### Summary

When a fee collector is configured on an ICRC-1 ledger, `icrc2_approve` transaction fees are always burned (destroyed, added to the token pool) rather than credited to the fee collector. The `fee_collector` context is extracted at the start of `Transaction::apply()` and correctly used for `Transfer` operations, but the `Approve` branch ignores it entirely, calling `balances_mut().burn()` unconditionally.

---

### Finding Description

In `rs/ledger_suite/icrc1/src/lib.rs`, the `Transaction::apply()` function begins by extracting the configured fee collector: [1](#0-0) 

For `Operation::Transfer`, this `fee_collector` is correctly forwarded to `balances_mut().transfer()`, which credits the fee to the collector if one is set: [2](#0-1) [3](#0-2) 

However, for `Operation::Approve`, the fee is handled by calling `balances_mut().burn()` directly, which **always** adds the fee to the `token_pool` (destroys it), completely ignoring the `fee_collector` variable: [4](#0-3) [5](#0-4) 

The `balances.burn()` function has no `fee_collector` parameter and unconditionally routes the amount to `token_pool`. There is no code path in the `Approve` branch that credits the fee collector.

This is explicitly confirmed by a test comment in the index-ng test suite: [6](#0-5) 

The index-ng canister's `process_balance_changes` also reflects this asymmetry: for `Operation::Approve`, it only credits `get_fee_collector_107()` (the newer ICRC-107 fee collector), never the legacy fee collector, while `Transfer` credits whichever is active: [7](#0-6) 

---

### Impact Explanation

When a fee collector is configured on an ICRC-1 ledger (a standard production configuration), every `icrc2_approve` call burns the fee instead of crediting it to the fee collector. The fee collector receives zero approve fees. The total token supply decreases with each approve (as if no fee collector were set), which is incorrect — when a fee collector is configured, fees should be preserved in circulation by crediting the collector. This is a ledger conservation bug: tokens are permanently destroyed that should instead be transferred to the fee collector account.

---

### Likelihood Explanation

`icrc2_approve` is a standard, publicly callable endpoint on any ICRC-2 compliant ledger. Any unprivileged user can trigger this code path simply by calling `icrc2_approve`. The fee collector configuration is a legitimate and commonly deployed feature of ICRC-1 ledgers on the Internet Computer. Every approve transaction on a ledger with a fee collector configured silently misdirects the fee.

---

### Recommendation

In the `Operation::Approve` branch of `Transaction::apply()` in `rs/ledger_suite/icrc1/src/lib.rs`, replace the unconditional `balances_mut().burn(from, fee)` call with a conditional: if `fee_collector` is `Some`, call `balances_mut().transfer(from, fee_collector_account, zero_amount, fee, Some(fee_collector_account))` or add a dedicated `balances_mut().collect_fee(from, fee, fee_collector)` helper that mirrors the logic in `Balances::transfer()`. If `fee_collector` is `None`, retain the burn behavior.

---

### Proof of Concept

1. Deploy an ICRC-1/ICRC-2 ledger with a `fee_collector_account` set (e.g., account `FC`).
2. Mint tokens to user `A`.
3. Record `total_supply_before` and `balance_of(FC)` before the approve.
4. User `A` calls `icrc2_approve` granting an allowance to spender `B`, paying the standard fee.
5. Observe: `total_supply` decreases by `fee` (tokens burned), and `balance_of(FC)` is unchanged.
6. Expected: `total_supply` unchanged, `balance_of(FC)` increased by `fee`.

The root cause is at: [8](#0-7)

### Citations

**File:** rs/ledger_suite/icrc1/src/lib.rs (L456-457)
```rust
        let fee_collector = context.fee_collector().map(|fc| fc.fee_collector);
        let fee_collector = fee_collector.as_ref();
```

**File:** rs/ledger_suite/icrc1/src/lib.rs (L468-474)
```rust
                    context.balances_mut().transfer(
                        from,
                        to,
                        amount.clone(),
                        fee,
                        fee_collector,
                    )?;
```

**File:** rs/ledger_suite/icrc1/src/lib.rs (L529-558)
```rust
            Operation::Approve {
                from,
                spender,
                amount,
                expected_allowance,
                expires_at,
                fee,
            } => {
                context
                    .balances_mut()
                    .burn(from, fee.clone().unwrap_or(effective_fee.clone()))?;
                let result = context
                    .approvals_mut()
                    .approve(
                        from,
                        spender,
                        amount.clone(),
                        expires_at.map(TimeStamp::from_nanos_since_unix_epoch),
                        now,
                        expected_allowance.clone(),
                    )
                    .map_err(TxApplyError::from);
                if let Err(e) = result {
                    context
                        .balances_mut()
                        .mint(from, fee.clone().unwrap_or(effective_fee))
                        .expect("bug: failed to refund approval fee");
                    return Err(e);
                }
            }
```

**File:** rs/ledger_suite/common/ledger_core/src/balances.rs (L117-128)
```rust
        match fee_collector {
            None => {
                // NB. integer overflow is not possible here unless there is a
                // severe bug in the system: total amount of tokens in the
                // circulation cannot exceed Tokens::max_value().
                self.token_pool = self
                    .token_pool
                    .checked_add(&fee)
                    .expect("Overflow while adding the fee to the token pool");
            }
            Some(fee_collector) => self.credit(fee_collector, fee),
        }
```

**File:** rs/ledger_suite/common/ledger_core/src/balances.rs (L132-143)
```rust
    pub fn burn(
        &mut self,
        from: &S::AccountId,
        amount: S::Tokens,
    ) -> Result<(), BalanceError<S::Tokens>> {
        self.debit(from, amount.clone())?;
        self.token_pool = self
            .token_pool
            .checked_add(&amount)
            .expect("Overflow of the token pool while burning");
        Ok(())
    }
```

**File:** rs/ledger_suite/icrc1/index-ng/tests/tests.rs (L1415-1417)
```rust
    // Legacy fee collector does not collect approve fees
    block_id = add_approve_block(block_id, Some(feecol_legacy));
    assert_eq!(2, icrc1_balance_of(env, index_id, feecol_legacy));
```

**File:** rs/ledger_suite/icrc1/index-ng/src/main.rs (L1116-1120)
```rust
                debit(block_index, from, fee);

                if let Some(fee_collector_107) = get_fee_collector_107().flatten() {
                    credit(block_index, fee_collector_107, fee);
                }
```
