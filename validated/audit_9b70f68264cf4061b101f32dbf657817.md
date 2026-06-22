### Title
Silent Allowance Clamping to `u64::MAX` in `icrc2_approve` — (`rs/ledger_suite/icrc1/ledger/src/main.rs`)

---

### Summary

An unprivileged caller can submit an `icrc2_approve` call with `amount` encoded as a `Nat` larger than `u64::MAX`. Instead of being rejected, the oversized value is silently clamped to `Tokens::max_value()` (`u64::MAX` e8s) and recorded as the spender's allowance. The owner never consented to granting `u64::MAX` tokens; the call should have been rejected with an explicit error.

---

### Finding Description

`icrc2_approve_not_async` in `rs/ledger_suite/icrc1/ledger/src/main.rs` converts the caller-supplied `amount: Nat` to `Tokens` at line 840:

```rust
let amount = Tokens::try_from(arg.amount).unwrap_or_else(|_| Tokens::max_value());
``` [1](#0-0) 

`TryFrom<Nat> for Tokens` returns `Err` for any value that does not fit in `u64`:

```rust
fn try_from(value: Nat) -> Result<Self, Self::Error> {
    match value.0.to_u64() {
        Some(e8s) => Ok(Self { e8s }),
        None => Err(format!("value {value} is bigger than Tokens::max_value()")),
    }
}
``` [2](#0-1) 

When `to_u64()` returns `None` (i.e., `amount > u64::MAX`), the `Err` is swallowed by `unwrap_or_else` and replaced with `Tokens::max_value()` — `u64::MAX` e8s — which is then stored as the approved allowance.

The inconsistency is visible in the same function: the `expected_allowance` field undergoes the same `TryFrom` conversion but correctly propagates the error:

```rust
Err(_) => {
    ...
    return Err(ApproveError::AllowanceChanged { current_allowance: ... });
}
``` [3](#0-2) 

The `amount` path has no equivalent guard.

---

### Impact Explanation

A spender whose allowance is set to `u64::MAX` e8s can call `icrc2_transfer_from` repeatedly to drain the owner's entire balance. The owner did not authorize this; they submitted a value that should have been rejected. The effective impact is unauthorized transfer of the owner's full balance to any destination the spender chooses.

---

### Likelihood Explanation

The attack requires only a single ingress `icrc2_approve` call with a Candid-encoded `Nat` value of `2^64` (or any value `> u64::MAX`). No privileged access, key material, or governance majority is needed. Any ICRC-2 ledger user can trigger this against their own account, granting an attacker-controlled spender `u64::MAX` allowance.

---

### Recommendation

Replace the silent fallback with an explicit error return:

```rust
let amount = Tokens::try_from(arg.amount).map_err(|_| ApproveError::GenericError {
    error_code: Nat::from(0u64),
    message: "amount exceeds maximum token value".to_string(),
})?;
```

This mirrors the correct handling already applied to `expected_allowance` on lines 842–852. [1](#0-0) 

---

### Proof of Concept

1. Encode `ApproveArgs { amount: Nat(u64::MAX) + 1, spender: attacker_account, ... }` as Candid.
2. Submit as an ingress `icrc2_approve` update call from any principal (the owner).
3. Call `icrc2_allowance({ account: owner, spender: attacker_account })`.
4. Observe returned allowance = `u64::MAX` e8s, not a rejection.
5. Call `icrc2_transfer_from` from the attacker principal to drain the owner's balance up to `u64::MAX` e8s.

### Citations

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L840-840)
```rust
        let amount = Tokens::try_from(arg.amount).unwrap_or_else(|_| Tokens::max_value());
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L842-852)
```rust
            Some(n) => match Tokens::try_from(n) {
                Ok(n) => Some(n),
                Err(_) => {
                    let current_allowance = ledger
                        .approvals()
                        .allowance(&from_account, &arg.spender, now)
                        .amount;
                    return Err(ApproveError::AllowanceChanged {
                        current_allowance: current_allowance.into(),
                    });
                }
```

**File:** rs/ledger_suite/common/ledger_core/src/tokens.rs (L291-296)
```rust
    fn try_from(value: Nat) -> Result<Self, Self::Error> {
        match value.0.to_u64() {
            Some(e8s) => Ok(Self { e8s }),
            None => Err(format!("value {value} is bigger than Tokens::max_value()")),
        }
    }
```
