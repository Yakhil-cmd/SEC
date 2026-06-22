### Title
Canister Trap via `.expect()` on Out-of-Range `utc_offset_minutes` in `add_expiration` - (`packages/icrc-ledger-types/src/icrc21/responses.rs`)

---

### Summary

The `add_expiration` method in `ConsentMessage` calls `time::UtcOffset::from_whole_seconds(...).expect("Invalid offset")` without validating that the caller-supplied `utc_offset_minutes` value falls within the range accepted by the `time` crate (±86399 seconds, i.e., ±1439 minutes). Any unprivileged ingress caller can supply a `ConsentMessageRequest` with `utc_offset_minutes` outside ±1439 and `expires_at = Some(...)` to force a canister trap in the `icrc21_canister_call_consent_message` `#[update]` endpoint, violating the ICRC-21 invariant that the endpoint must never trap on well-typed input.

---

### Finding Description

`ConsentMessageMetadata.utc_offset_minutes` is typed as `Option<i16>`, accepting any value in [-32768, 32767] with no validation at the Candid deserialization boundary or anywhere in the call chain. [1](#0-0) 

When `ConsentMessageBuilder::build` processes an `icrc2_approve` call, it unconditionally calls: [2](#0-1) 

Inside `add_expiration`, the `GenericDisplayMessage` branch executes this code whenever `expires_at` is `Some(...)`: [3](#0-2) 

`time::UtcOffset::from_whole_seconds` returns `Err` for any value outside the range (-86400, 86400) exclusive. With `utc_offset_minutes = 1440`, the computation is `1440 * 60 = 86400`, which is exactly at the boundary and rejected. The `.expect("Invalid offset")` then panics, causing a canister trap.

The `FieldsDisplayMessage` branch is **not** affected — it ignores `utc_offset_minutes` entirely. [4](#0-3) 

---

### Impact Explanation

A canister trap in an `#[update]` method on the Internet Computer causes the execution to be rolled back and the caller receives a system-level reject, not a graceful `Err(Icrc21Error)`. This breaks the ICRC-21 contract. Any wallet or dApp that calls `icrc21_canister_call_consent_message` with a timezone offset ≥ UTC+24:00 (e.g., a buggy or malicious client sending `utc_offset_minutes = 1440`) will receive a trap instead of a structured error, degrading UX and potentially blocking consent-message-gated approval flows.

---

### Likelihood Explanation

The attack requires only a well-typed Candid message — no privileged access, no key material, no governance majority. The `i16` type permits values up to 32767, and the valid range is only ±1439. Any caller (including anonymous) can trigger this. The affected ledgers (`icrc1/ledger`, `icp/ledger`, `ckbtc/minter`, `ckdoge/minter`) all expose this endpoint. [5](#0-4) 

---

### Recommendation

Replace `.expect("Invalid offset")` with a graceful error return. Since `add_expiration` currently returns `()`, its signature should be changed to `Result<(), Icrc21Error>`, or the offset validation should be done upstream in `build()` before calling `add_expiration`. Example fix at the call site:

```rust
let offset = match time::UtcOffset::from_whole_seconds(
    (utc_offset_minutes.unwrap_or(0) as i32) * 60,
) {
    Ok(o) => o,
    Err(_) => return format!("Invalid UTC offset: {}", utc_offset_minutes.unwrap_or(0)),
};
```

Alternatively, validate `utc_offset_minutes` in `build()` and return `Err(Icrc21Error::GenericError { ... })` before reaching `add_expiration`.

---

### Proof of Concept

```rust
#[test]
fn test_out_of_range_utc_offset_traps() {
    use std::panic::catch_unwind;
    // Triggers the panic: utc_offset_minutes=1440, expires_at=Some(...)
    let result = catch_unwind(|| {
        let mut msg = ConsentMessage::GenericDisplayMessage(String::new());
        msg.add_expiration(Some(1_000_000_000_000_u64), Some(1440_i16));
    });
    // Should return Ok(...) or Err(...), not panic
    assert!(result.is_ok(), "add_expiration panicked on utc_offset_minutes=1440");
}
```

Values `{1440, -1440, 32767, -32768}` all trigger the panic when `expires_at` is `Some(...)` and `display_type` is `GenericDisplay` (the default). [6](#0-5)

### Citations

**File:** packages/icrc-ledger-types/src/icrc21/requests.rs (L5-8)
```rust
pub struct ConsentMessageMetadata {
    pub language: String,
    pub utc_offset_minutes: Option<i16>,
}
```

**File:** packages/icrc-ledger-types/src/icrc21/lib.rs (L241-241)
```rust
                message.add_expiration(self.expires_at, self.utc_offset_minutes);
```

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L227-260)
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
```

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L262-280)
```rust
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
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L35-38)
```rust
use icrc_ledger_types::icrc21::{
    errors::Icrc21Error, lib::build_icrc21_consent_info_for_icrc1_and_icrc2_endpoints,
    requests::ConsentMessageRequest, responses::ConsentInfo,
};
```
