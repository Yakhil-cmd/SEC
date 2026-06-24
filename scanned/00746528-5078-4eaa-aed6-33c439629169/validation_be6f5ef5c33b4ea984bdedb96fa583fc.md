The bug is real and the code path is fully confirmed. Here is the analysis:

---

### Title
ICRC-21 `add_expiration` FieldsDisplay Branch Silently Wraps `expires_at > i64::MAX`, Displaying a Fabricated Timestamp to Hardware Wallet Users — (`packages/icrc-ledger-types/src/icrc21/responses.rs`)

### Summary

The `FieldsDisplay` branch of `add_expiration` performs an unchecked `u64 → i64` cast on the nanosecond timestamp, producing a silently wrong `TimestampSeconds` value when `expires_at > i64::MAX`. The `GenericDisplay` branch handles the same overflow by returning an explicit error string; the `FieldsDisplay` branch does not. Any unprivileged caller can trigger this by sending a `ConsentMessageRequest` for `icrc2_approve` with a crafted `expires_at` and `device_spec = FieldsDisplay`.

### Finding Description

In `add_expiration`, the `FieldsDisplay` arm at lines 264–271:

```rust
let seconds = (expires_at as i64) / 10_i64.pow(9);
fields_display.fields.push((
    "Approval expiration".to_string(),
    Value::TimestampSeconds { amount: seconds as u64 },
))
``` [1](#0-0) 

The cast `expires_at as i64` is a Rust wrapping (bit-reinterpretation) cast. For any `expires_at > i64::MAX as u64`, the result is negative. The subsequent `seconds as u64` then wraps back to a large positive value. Two concrete cases:

| `expires_at` (u64) | `as i64` | `/ 1e9` (seconds) | `as u64` (displayed) | Displayed date |
|---|---|---|---|---|
| `u64::MAX` (18446744073709551615) | `-1` | `0` | `0` | **Unix epoch — Jan 1, 1970** |
| `i64::MAX + 1` (9223372036854775808) | `i64::MIN` | `-9223372036` | `18446744064486776320` | **~584 billion years in the future** |

The `GenericDisplay` branch at lines 232–243 handles the same inputs correctly: the `time::OffsetDateTime::from_unix_timestamp` call fails for out-of-range seconds, and the nanosecond remainder overflows `u32` range, both of which return explicit `"Invalid timestamp"` / `"Invalid nanosecond"` strings. [2](#0-1) 

The `expires_at` value flows from the Candid-decoded `ApproveArgs` directly into `add_expiration` with no range check: [3](#0-2) 

### Impact Explanation

The ICRC-21 consent message is the **only** security signal a hardware wallet has before the user signs. The `FieldsDisplay` variant is specifically designed for structured rendering on constrained devices (Ledger, etc.).

The most dangerous concrete case is `expires_at = u64::MAX`:

1. A malicious dApp constructs `icrc2_approve` with `expires_at = u64::MAX` — a valid `nat64` that the ledger accepts (it is far in the future; the `expires_at <= now` guard in `approvals.rs` passes).
2. The dApp sends `icrc21_consent_message` with `device_spec = FieldsDisplay`.
3. The returned `TimestampSeconds { amount: 0 }` causes the hardware wallet to display **"Approval expiration: Jan 1, 1970"** — a date in the past.
4. The user interprets this as a harmless, already-expired approval and signs.
5. The on-chain approval is permanent (`expires_at = u64::MAX`), granting the spender an indefinite allowance. [4](#0-3) 

### Likelihood Explanation

- The `icrc21_consent_message` endpoint is a public query; no authentication is required to call it.
- The `arg` blob is fully attacker-controlled (it is the Candid encoding of `ApproveArgs`).
- `u64::MAX` is a valid `nat64` value; the ledger imposes no upper bound on `expires_at` beyond "must be in the future."
- Hardware wallets that implement `FieldsDisplay` (the structured path) are the intended production consumers of this code path.

### Recommendation

Replace the unchecked cast with an explicit range check before converting:

```rust
Some(expires_at) => {
    if expires_at > i64::MAX as u64 {
        fields_display.fields.push((
            "Approval expiration".to_string(),
            Value::Text { content: format!("Invalid timestamp: {expires_at}") },
        ));
    } else {
        let seconds = (expires_at as i64) / 1_000_000_000_i64;
        fields_display.fields.push((
            "Approval expiration".to_string(),
            Value::TimestampSeconds { amount: seconds as u64 },
        ));
    }
}
```

This mirrors the error-handling already present in the `GenericDisplay` branch.

### Proof of Concept

```rust
// State-machine / unit test
let expires_at: u64 = u64::MAX;
let mut msg = ConsentMessage::FieldsDisplayMessage(Default::default());
msg.add_expiration(Some(expires_at), None);

if let ConsentMessage::FieldsDisplayMessage(fd) = &msg {
    let (_, value) = fd.fields.iter()
        .find(|(k, _)| k == "Approval expiration").unwrap();
    // BUG: displays Unix epoch (0) instead of an error
    assert_eq!(*value, Value::TimestampSeconds { amount: 0 });
    // A hardware wallet renders this as "Jan 1, 1970" — past expiration
}
``` [5](#0-4)

### Citations

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L227-282)
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

                        // Apply the offset minutes
                        let offset = time::UtcOffset::from_whole_seconds(
                            (utc_offset_minutes.unwrap_or(0) * 60).into(),
                        )
                        .expect("Invalid offset");
                        let offset_dt = utc_dt.to_offset(offset);

                        // Format as a string including the offset
                        match offset_dt.format(&time::format_description::well_known::Rfc2822) {
                            Ok(formatted) => formatted,
                            Err(_) => format!("Invalid timestamp: {ts}"),
                        }
                    })
                    .unwrap_or("This approval does not have an expiration.".to_owned());
                message.push_str(&format!("\n\n**Approval expiration:**\n{expires_at}"));
            }
            ConsentMessage::FieldsDisplayMessage(fields_display) => {
                match expires_at {
                    Some(expires_at) => {
                        let seconds = (expires_at as i64) / 10_i64.pow(9);
                        fields_display.fields.push((
                            "Approval expiration".to_string(),
                            Value::TimestampSeconds {
                                amount: seconds as u64,
                            },
                        ))
                    }
                    None => fields_display.fields.push((
                        "Approval expiration".to_string(),
                        Value::Text {
                            content: "This approval does not have an expiration.".to_string(),
                        },
                    )),
                };
            }
        }
    }
```

**File:** packages/icrc-ledger-types/src/icrc21/lib.rs (L462-464)
```rust
            if let Some(expires_at) = expires_at {
                display_message_builder = display_message_builder.with_expires_at(expires_at);
            }
```
