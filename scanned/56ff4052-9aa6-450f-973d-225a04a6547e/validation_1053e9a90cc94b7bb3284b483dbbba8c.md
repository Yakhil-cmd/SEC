### Title
ICP Ledger `apply_operation` Applies Allowance Check on Self-Burn via `icrc2_transfer_from` When `spender == from` - (`File: rs/ledger_suite/icp/src/lib.rs`)

---

### Summary

In the ICP ledger's `apply_operation` function, the `Operation::Burn` branch unconditionally checks the allowance whenever `spender` is `Some`, even when `spender == from` (i.e., the account owner is calling `icrc2_transfer_from` to burn their own tokens). The `Operation::Transfer` branch in the same function correctly bypasses the allowance check when `spender == from`, but the `Burn` branch does not. This means any user calling `icrc2_transfer_from` to burn their own ICP (send to the minting account) will always receive `InsufficientAllowance { allowance: 0 }`, even though they are the rightful account owner.

---

### Finding Description

In `rs/ledger_suite/icp/src/lib.rs`, `apply_operation` handles `Operation::Burn` as follows:

```rust
Operation::Burn { from, amount, spender } => {
    if let Some(spender) = spender.as_ref() {          // ← runs for ALL spender.is_some()
        let allowance = context.approvals().allowance(from, spender, now);
        if allowance.amount < *amount {
            return Err(TxApplyError::InsufficientAllowance { allowance: allowance.amount });
        }
    }
    context.balances_mut().burn(from, *amount)?;
    if spender.is_some() && from != &spender.unwrap() { // ← correctly guards use_allowance
        context.approvals_mut().use_allowance(...);
    }
}
``` [1](#0-0) 

The allowance **check** at line 152 fires for any `spender.is_some()`, including the case `from == spender`. But the allowance **deduction** at line 161 correctly guards with `from != spender`. This is inconsistent.

Contrast with the `Operation::Transfer` branch in the same file, which correctly short-circuits the entire allowance path when `spender == from`:

```rust
if spender.is_none() || *from == spender.unwrap() {
    // bypass allowance check for self-transfer_from
    context.balances_mut().transfer(from, to, *amount, *fee, None)?;
    return Ok(());
}
``` [2](#0-1) 

The ICRC-1 ledger's `Transaction::apply` correctly guards the Burn allowance check with `from != &spender.unwrap()`:

```rust
if spender.is_some() && from != &spender.unwrap() {
    let allowance = context.approvals().allowance(from, &spender.unwrap(), now);
    ...
}
``` [3](#0-2) 

The ICP ledger's `icrc2_transfer_from` endpoint constructs `spender_account` from `caller()` and passes it to `icrc1_send_not_async`, which converts both `from_account` and `spender_account` to `AccountIdentifier` before building the `Operation::Burn`: [4](#0-3) [5](#0-4) 

When `from_account == spender_account` (same principal, same subaccount), both convert to the same `AccountIdentifier`, so `Operation::Burn { from: alice_id, spender: Some(alice_id), ... }` is produced. The allowance check then queries `allowance(alice_id, alice_id, now)`, which returns 0 (self-approvals are not stored), and since `0 < amount` for any non-zero burn, the call always returns `InsufficientAllowance { allowance: 0 }`.

---

### Impact Explanation

Any user who calls `icrc2_transfer_from` on the ICP ledger with `from` equal to their own account and `to` equal to the minting account (a burn) will always fail with `InsufficientAllowance`, even though they are the account owner. This breaks the pull-only integration pattern (protocols that exclusively use `transfer_from` rather than mixing `transfer` and `transfer_from`). Concretely:

- A canister or protocol that uses `icrc2_transfer_from` to pull ICP from a user and then burn it (e.g., for neuron staking, fee payment, or token destruction) cannot do so when the user is the caller.
- Tokens can become permanently stranded in protocols that rely on `transfer_from` for burn flows.

---

### Likelihood Explanation

The entry path is a standard unprivileged ingress call to `icrc2_transfer_from` on the ICP ledger canister. No special permissions, admin keys, or governance majority are required. Any user or canister that attempts a self-burn via `transfer_from` will trigger this. The likelihood is high for any protocol that adopts the pull-only pattern, which is common in DeFi integrations.

---

### Recommendation

Add the same `from == spender` guard to the `Burn` branch in `apply_operation` that already exists in the `Transfer` branch:

```rust
Operation::Burn { from, amount, spender } => {
    if let Some(spender) = spender.as_ref() {
+       if from != spender {
            let allowance = context.approvals().allowance(from, spender, now);
            if allowance.amount < *amount {
                return Err(TxApplyError::InsufficientAllowance { allowance: allowance.amount });
            }
+       }
    }
    context.balances_mut().burn(from, *amount)?;
    if spender.is_some() && from != &spender.unwrap() {
        context.approvals_mut().use_allowance(from, &spender.unwrap(), *amount, now)
            .expect("bug: cannot use allowance");
    }
}
```

This mirrors the fix already present in the ICRC-1 ledger at `rs/ledger_suite/icrc1/src/lib.rs` line 507.

---

### Proof of Concept

1. Alice holds ICP in her default account `{ owner: Alice, subaccount: None }`.
2. Alice calls `icrc2_transfer_from` on the ICP ledger with:
   - `from = { owner: Alice, subaccount: None }`
   - `to = minting_account` (burn destination)
   - `spender_subaccount = None`
   - `amount = 10_000` (any non-zero amount)
3. The ledger constructs `spender_account = { owner: Alice, subaccount: None }`.
4. Both `from` and `spender_account` convert to the same `AccountIdentifier` (`alice_id`).
5. `Operation::Burn { from: alice_id, amount: 10_000, spender: Some(alice_id) }` is created.
6. `apply_operation` enters the `Burn` branch, finds `spender.is_some()` = true, queries `allowance(alice_id, alice_id, now)` = 0.
7. `0 < 10_000` → returns `Err(TxApplyError::InsufficientAllowance { allowance: 0 })`.
8. Alice's burn fails despite her being the account owner, while an identical call using `icrc1_transfer` to the minting account succeeds.

### Citations

**File:** rs/ledger_suite/icp/src/lib.rs (L147-166)
```rust
        Operation::Burn {
            from,
            amount,
            spender,
        } => {
            if let Some(spender) = spender.as_ref() {
                let allowance = context.approvals().allowance(from, spender, now);
                if allowance.amount < *amount {
                    return Err(TxApplyError::InsufficientAllowance {
                        allowance: allowance.amount,
                    });
                }
            }
            context.balances_mut().burn(from, *amount)?;
            if spender.is_some() && from != &spender.unwrap() {
                context
                    .approvals_mut()
                    .use_allowance(from, &spender.unwrap(), *amount, now)
                    .expect("bug: cannot use allowance");
            }
```

**File:** rs/ledger_suite/icp/src/lib.rs (L210-224)
```rust
            if spender.is_none() || *from == spender.unwrap() {
                // It is either a regular transfer or a self-transfer_from.

                // NB. We bypass the allowance check if the account owner calls
                // transfer_from.

                // NB. We cannot reliably detect self-transfer_from at this level.
                // We need help from the transfer_from endpoint to populate
                // [from] and [spender] with equal values if the spender is the
                // account owner.
                context
                    .balances_mut()
                    .transfer(from, to, *amount, *fee, None)?;
                return Ok(());
            }
```

**File:** rs/ledger_suite/icrc1/src/lib.rs (L507-513)
```rust
                if spender.is_some() && from != &spender.unwrap() {
                    let allowance = context.approvals().allowance(from, &spender.unwrap(), now);
                    if allowance.amount < *amount {
                        return Err(TxApplyError::InsufficientAllowance {
                            allowance: allowance.amount,
                        });
                    }
```

**File:** rs/ledger_suite/icp/ledger/src/main.rs (L274-320)
```rust
) -> Result<BlockIndex, CoreTransferError<Tokens>> {
    let from = AccountIdentifier::from(from_account);
    let to = AccountIdentifier::from(to_account);
    match memo.as_ref() {
        Some(memo) if memo.0.len() > MEMO_SIZE_BYTES => trap("the memo field is too large"),
        _ => {}
    };
    let amount = match amount.0.to_u64() {
        Some(n) => Tokens::from_e8s(n),
        None => {
            // No one can have so many tokens
            let balance = account_balance(from);
            assert!(balance.get_e8s() < amount);
            return Err(CoreTransferError::InsufficientFunds { balance });
        }
    };
    let created_at_time = created_at_time.map(TimeStamp::from_nanos_since_unix_epoch);
    let minting_acc = LEDGER
        .read()
        .unwrap()
        .minting_account_id
        .expect("Minting canister id not initialized");
    let now = TimeStamp::from_nanos_since_unix_epoch(time());
    let (operation, effective_fee) = if to == minting_acc {
        if fee.is_some() && fee.as_ref() != Some(&Nat::from(0_u64)) {
            return Err(CoreTransferError::BadFee {
                expected_fee: Tokens::ZERO,
            });
        }
        let ledger = LEDGER.read().unwrap();
        let balance = ledger.balances().account_balance(&from);
        let min_burn_amount = ledger.transfer_fee.min(balance);
        if amount < min_burn_amount {
            return Err(CoreTransferError::BadBurn { min_burn_amount });
        }
        if amount == Tokens::ZERO {
            return Err(CoreTransferError::BadBurn {
                min_burn_amount: ledger.transfer_fee,
            });
        }
        (
            Operation::Burn {
                from,
                amount,
                spender: spender_account.map(AccountIdentifier::from),
            },
            Tokens::ZERO,
```

**File:** rs/ledger_suite/icp/ledger/src/main.rs (L858-870)
```rust
    let spender_account = Account {
        owner: caller(),
        subaccount: arg.spender_subaccount,
    };
    Ok(Nat::from(
        icrc1_send(
            arg.memo,
            arg.amount,
            arg.fee,
            arg.from,
            arg.to,
            Some(spender_account),
            arg.created_at_time,
```
