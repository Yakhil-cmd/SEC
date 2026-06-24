### Title
Signed Cast Overflow in `add_expiration` Produces Misleading Consent Message for `expires_at > i64::MAX` — (`packages/icrc-ledger-types/src/icrc21/responses.rs`)

---

### Summary

`add_expiration` casts the caller-supplied `expires_at: u64` to `i64` without bounds-checking. For any value above `i64::MAX` (e.g., `u64::MAX`), the cast wraps to a negative number, producing a nanosecond remainder that overflows `u32`, which causes `replace_nanosecond` to return `Err`, and the consent message displays `"Invalid nanosecond: 4294967295"` instead of the actual expiration date. The `FieldsDisplay` path has a symmetric bug: it silently shows Unix epoch (`amount: 0`) for the same input.

---

### Finding Description

The vulnerable code is in `add_expiration`, `GenericDisplayMessage` branch:

```rust
// responses.rs lines 232-233
let seconds = (ts as i64) / 10_i64.pow(9);
let nanos   = ((ts as i64) % 10_i64.pow(9)) as u32;
``` [1](#0-0) 

For `ts = u64::MAX = 18_446_744_073_709_551_615`:

| Step | Expression | Result |
|---|---|---|
| wrapping cast | `u64::MAX as i64` | `-1` |
| seconds | `(-1) / 1_000_000_000` | `0` (truncates toward zero) |
| remainder | `(-1) % 1_000_000_000` | `-1` (Rust: sign follows dividend) |
| nanos cast | `(-1_i64) as u32` | `4_294_967_295` (wraps to `u32::MAX`) |
| `from_unix_timestamp(0)` | epoch | `Ok` |
| `replace_nanosecond(4_294_967_295)` | > 999_999_999 | `Err` | [2](#0-1) 

Result: the `GenericDisplayMessage` consent message contains `"Invalid nanosecond: 4294967295"` instead of a valid RFC-2822 date string.

The `FieldsDisplayMessage` branch has a parallel bug:

```rust
// responses.rs lines 265-270
let seconds = (expires_at as i64) / 10_i64.pow(9);  // = 0 for u64::MAX
fields_display.fields.push((
    "Approval expiration".to_string(),
    Value::TimestampSeconds { amount: seconds as u64 },  // = 0 = Unix epoch
))
``` [3](#0-2) 

This silently displays Unix epoch (Jan 1, 1970) as the expiration, when the actual expiration is year ~2554.

The `expires_at` value flows directly from the caller-supplied `ApproveArgs` with no sanitization before reaching `add_expiration`: [4](#0-3) 

---

### Impact Explanation

ICRC-21 consent messages exist specifically so that wallets can show users an accurate, human-readable summary of what they are signing. A bug that corrupts the expiration display undermines this security guarantee:

- **GenericDisplay**: user sees `"Invalid nanosecond: 4294967295"` — confusing, but may cause the user to dismiss the error and approve anyway.
- **FieldsDisplay**: user sees `"Approval expiration: Jan 1, 1970"` — actively misleading. A user could reasonably interpret this as "the approval expires immediately / is already expired and therefore harmless," and approve an allowance that is actually valid for ~584 years.

The actual `icrc2_approve` ledger call is unaffected — the ledger stores and enforces `expires_at` correctly. The damage is entirely in the consent message layer, causing users to make approval decisions based on false information.

---

### Likelihood Explanation

Any unprivileged principal can call `icrc21_consent_message` with a crafted `icrc2_approve` arg containing `expires_at = u64::MAX`. No special role, key, or governance majority is required. The call is a standard ingress/query endpoint exposed by every ICRC-2 ledger that implements ICRC-21.

---

### Recommendation

Replace the unchecked `as i64` cast with a checked conversion and return an error string if the value is out of range:

```rust
let ts_i64 = i64::try_from(ts).unwrap_or_else(|_| {
    return format!("Invalid timestamp: {ts}");
});
let seconds = ts_i64 / 1_000_000_000;
let nanos   = (ts_i64 % 1_000_000_000) as u32;  // safe: remainder is 0..999_999_999
```

Apply the same fix to the `FieldsDisplayMessage` branch at line 265.

---

### Proof of Concept

```rust
#[test]
fn test_add_expiration_u64_max() {
    let mut msg = ConsentMessage::GenericDisplayMessage(String::new());
    msg.add_expiration(Some(u64::MAX), None);
    if let ConsentMessage::GenericDisplayMessage(s) = &msg {
        // Must NOT contain "Invalid nanosecond"
        assert!(
            !s.contains("Invalid nanosecond"),
            "Consent message contains error string: {s}"
        );
    }
}
```

This test fails against the current code, producing:
```
Consent message contains error string:
**Approval expiration:**
Invalid nanosecond: 4294967295
``` [5](#0-4)

### Citations

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L227-244)
```rust
    pub fn add_expiration(&mut self, expires_at: Option<u64>, utc_offset_minutes: Option<i16>) {
        match self {
            ConsentMessage::GenericDisplayMessage(message) => {
                let expires_at = expires_at
                    .map(|ts| {
                        let seconds = (ts as i64) / 10_i64.pow(9);
                        let nanos = ((ts as i64) % 10_i64.pow(9)) as u32;

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

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L265-270)
```rust
                        let seconds = (expires_at as i64) / 10_i64.pow(9);
                        fields_display.fields.push((
                            "Approval expiration".to_string(),
                            Value::TimestampSeconds {
                                amount: seconds as u64,
                            },
```

**File:** packages/icrc-ledger-types/src/icrc21/lib.rs (L462-464)
```rust
            if let Some(expires_at) = expires_at {
                display_message_builder = display_message_builder.with_expires_at(expires_at);
            }
```
