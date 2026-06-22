The code is present and I have everything needed for a complete analysis. Let me trace the exact arithmetic.

### Title
Silent Integer Wrap in `add_expiration` FieldsDisplay Branch Produces Wildly Incorrect Consent-Message Expiration Timestamp — (`packages/icrc-ledger-types/src/icrc21/responses.rs`)

---

### Summary

The `add_expiration` function in `responses.rs` has two divergent code paths for `GenericDisplay` and `FieldsDisplay`. When `expires_at > i64::MAX` (any `u64` value with the high bit set), the `FieldsDisplay` branch silently wraps through two successive unsafe casts and stores a completely wrong second-count in `Value::TimestampSeconds`, while the `GenericDisplay` branch correctly detects the same value as invalid and returns an error string. The ledger accepts any `u64` value for `expires_at` without range validation, so a malicious dApp can submit a well-formed `icrc2_approve` consent-message request with `expires_at = u64::MAX` and cause a `FieldsDisplay`-capable wallet to show the approval as expiring at Unix epoch (Jan 1 1970) while the on-chain approval actually never expires.

---

### Finding Description

**Root cause — `add_expiration`, FieldsDisplay branch:** [1](#0-0) 

```rust
Some(expires_at) => {
    let seconds = (expires_at as i64) / 10_i64.pow(9);   // ← cast wraps for expires_at > i64::MAX
    fields_display.fields.push((
        "Approval expiration".to_string(),
        Value::TimestampSeconds {
            amount: seconds as u64,   // ← negative seconds wraps again silently
        },
    ))
}
```

**Concrete arithmetic for `expires_at = u64::MAX`:**

| Step | Value |
|---|---|
| `u64::MAX as i64` | `-1` (two's-complement wrap) |
| `-1_i64 / 1_000_000_000` | `0` (Rust truncates toward zero) |
| `0_i64 as u64` | `0` |
| Displayed as | `TimestampSeconds { amount: 0 }` = Unix epoch, Jan 1 1970 |

**Concrete arithmetic for `expires_at = 9_223_372_036_854_775_808` (i64::MAX + 1):**

| Step | Value |
|---|---|
| `as i64` | `i64::MIN = -9_223_372_036_854_775_808` |
| `/ 1_000_000_000` | `-9_223_372_036` |
| `as u64` | `18_446_744_064_486_227_580` |
| Displayed as | `TimestampSeconds { amount: 18446744064486227580 }` = year ~584 billion |

**GenericDisplay branch for the same inputs correctly returns an error string:** [2](#0-1) 

For `u64::MAX`: `seconds=0` is valid, but `nanos = (-1 % 10^9) as u32 = 4_294_967_295` causes `replace_nanosecond` to fail → `"Invalid nanosecond: 4294967295"`.
For `i64::MAX+1`: `from_unix_timestamp(-9_223_372_036)` is out of range → `"Invalid timestamp: 9223372036854775808"`.

**The ledger accepts any `u64` for `expires_at` without range validation:** [3](#0-2) 

`expires_at: arg.expires_at` is forwarded verbatim. The only guard in the approvals layer is: [4](#0-3) 

`u64::MAX` is far in the future, so `expires_at <= now` is false and the approval is accepted and stored on-chain with `expires_at = u64::MAX`.

---

### Impact Explanation

A malicious dApp constructs an `icrc2_approve` call with `expires_at = u64::MAX` (effectively "never expires"). It requests `FieldsDisplay` from the wallet via `icrc21_canister_call_consent_message`. The wallet receives `Value::TimestampSeconds { amount: 0 }` and renders the approval expiration as **Jan 1, 1970** — making it appear the approval has already expired or is trivially short-lived. The user, believing the approval is harmless, signs it. The on-chain approval is stored with `expires_at = u64::MAX`, giving the spender unlimited time to drain the approved allowance up to the approved amount. This is a consent-message display integrity failure that can directly precede fund loss.

---

### Likelihood Explanation

The attack requires a malicious dApp that deliberately sets `expires_at > i64::MAX` and a wallet that uses `FieldsDisplay` (not the default `GenericDisplay`). `FieldsDisplay` is an explicitly supported variant in the ICRC-21 standard and is present in the production `.did` files for both the ICP ledger and the ICRC-1 ledger. The entrypoint is fully public and requires no privilege. The arithmetic is deterministic and locally testable.

---

### Recommendation

Replace the unsafe cast chain in the `FieldsDisplay` branch with unsigned division and add the same validity guard used by the `GenericDisplay` branch:

```rust
Some(expires_at) => {
    // Use unsigned division — no cast needed
    let seconds = expires_at / 1_000_000_000_u64;
    // Optionally mirror the GenericDisplay validity check:
    // if time::OffsetDateTime::from_unix_timestamp(seconds as i64).is_err() { ... }
    fields_display.fields.push((
        "Approval expiration".to_string(),
        Value::TimestampSeconds { amount: seconds },
    ))
}
```

This eliminates both the `u64 → i64` wrap and the subsequent `i64 → u64` wrap, and keeps the two display paths consistent.

---

### Proof of Concept

```rust
#[test]
fn test_add_expiration_fields_display_wrap() {
    use icrc_ledger_types::icrc21::responses::{ConsentMessage, FieldsDisplay, Value};

    let mut msg = ConsentMessage::FieldsDisplayMessage(FieldsDisplay::default());

    // expires_at = u64::MAX: as i64 = -1, -1/1e9 = 0, 0 as u64 = 0
    msg.add_expiration(Some(u64::MAX), None);
    if let ConsentMessage::FieldsDisplayMessage(ref fd) = msg {
        let (_, val) = fd.fields.last().unwrap();
        // CORRECT would be u64::MAX / 1_000_000_000 = 18_446_744_073
        // BUG produces 0 (Unix epoch)
        assert_eq!(
            *val,
            Value::TimestampSeconds { amount: u64::MAX / 1_000_000_000 },
            "FieldsDisplay wraps u64::MAX to epoch instead of correct seconds"
        );
    }
}
```

This test fails against the current code, confirming the wrap. A differential test comparing `GenericDisplay` and `FieldsDisplay` output for the same `expires_at = u64::MAX` will show the two paths disagree: one returns `"Invalid nanosecond: 4294967295"`, the other returns `TimestampSeconds { amount: 0 }`.

### Citations

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L235-244)
```rust
                        let utc_dt = match (match time::OffsetDateTime::from_unix_timestamp(seconds)
                        {
                            Ok(dt) => dt,
                            Err(_) => return format!("Invalid timestamp: {ts}"),
                        })
                        .replace_nanosecond(nanos)
                        {
                            Ok(dt) => dt,
                            Err(_) => return format!("Invalid nanosecond: {nanos}"),
                        };
```

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L264-271)
```rust
                    Some(expires_at) => {
                        let seconds = (expires_at as i64) / 10_i64.pow(9);
                        fields_display.fields.push((
                            "Approval expiration".to_string(),
                            Value::TimestampSeconds {
                                amount: seconds as u64,
                            },
                        ))
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L863-870)
```rust
        let tx = Transaction {
            operation: Operation::Approve {
                from: from_account,
                spender: arg.spender,
                amount,
                expected_allowance,
                expires_at: arg.expires_at,
                fee: arg.fee.map(|_| expected_fee_tokens),
```

**File:** rs/ledger_suite/common/ledger_core/src/approvals.rs (L247-249)
```rust
            if expires_at.unwrap_or_else(remote_future) <= now {
                return Err(ApproveError::ExpiredApproval { now });
            }
```
