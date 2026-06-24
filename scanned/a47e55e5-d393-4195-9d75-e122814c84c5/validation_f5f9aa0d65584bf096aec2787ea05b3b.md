I have the exact code. Let me verify the arithmetic path precisely.

The arithmetic is unambiguous. Here is the full trace and verdict.

---

### Title
`add_expiration` signed-cast overflow produces garbled consent-message expiration for `expires_at > i64::MAX` — (`packages/icrc-ledger-types/src/icrc21/responses.rs`)

### Summary

`add_expiration` converts a `u64` nanosecond timestamp to a human-readable date by casting it to `i64` before dividing. For any `expires_at > i64::MAX` (e.g. `u64::MAX`), the cast wraps to a negative value, the modulo remainder is also negative, and the subsequent `as u32` wrapping produces a nanosecond value ≥ 10⁹. `replace_nanosecond` rejects it, and the `GenericDisplay` consent message shows `"Invalid nanosecond: 4294967295"` instead of the actual expiration date.

### Finding Description

**Exact arithmetic for `ts = u64::MAX = 18_446_744_073_709_551_615`:**

| Step | Expression | Result |
|---|---|---|
| 1 | `u64::MAX as i64` | `-1` (wrapping) |
| 2 | `(-1_i64) / 1_000_000_000` | `0` (truncates toward zero) |
| 3 | `(-1_i64) % 1_000_000_000` | `-1` (Rust: sign follows dividend) |
| 4 | `(-1_i64) as u32` | `4_294_967_295` (wrapping) |
| 5 | `OffsetDateTime::from_unix_timestamp(0)` | `Ok(epoch)` ✓ |
| 6 | `.replace_nanosecond(4_294_967_295)` | `Err(...)` — valid range is 0..=999_999_999 |
| 7 | fallback branch | `"Invalid nanosecond: 4294967295"` |

The same failure occurs for **any** `ts > i64::MAX`: the cast is always negative, the `% 10⁹` remainder is always negative, and the `as u32` wrapping always exceeds the valid nanosecond range.

The buggy lines are: [1](#0-0) 

The `FieldsDisplay` branch at line 265 has the same cast but only stores `seconds as u64`, so it silently shows a wrong timestamp without an error string — a separate but related issue. [2](#0-1) 

### Impact Explanation

The `icrc_21_consent_message` endpoint is a public query callable by any unprivileged principal. A malicious dApp encodes `ApproveArgs { expires_at: Some(u64::MAX), ... }` and requests `GenericDisplay`. The user's wallet displays:

```
**Approval expiration:**
Invalid nanosecond: 4294967295
```

instead of the correct RFC-2822 date (~year 2554). The user cannot determine the actual expiration from the consent message. They may approve an effectively permanent allowance believing the expiration field is erroneous or harmless. The ICRC-21 consent message is the sole user-facing security gate before signing; corrupting it for a valid input directly undermines its purpose.

### Likelihood Explanation

- No privilege required; any caller can submit a `ConsentMessageRequest` with attacker-controlled `arg` bytes.
- The trigger value (`expires_at > i64::MAX`) is a valid `u64` accepted by the Candid interface.
- The path is deterministic and locally reproducible with a single unit test.

### Recommendation

Replace the unsafe `as i64` cast with a checked conversion:

```rust
// In add_expiration, GenericDisplay branch
let ts_i64 = i64::try_from(ts).unwrap_or_else(|_| {
    // ts > i64::MAX: compute seconds/nanos directly from u64
    ...
});
```

Or compute seconds and nanos entirely in `u64` arithmetic before converting:

```rust
let seconds_u64 = ts / 1_000_000_000u64;
let nanos_u64   = ts % 1_000_000_000u64;
let seconds = i64::try_from(seconds_u64)
    .unwrap_or_else(|_| return format!("Invalid timestamp: {ts}"));
let nanos   = nanos_u64 as u32; // safe: always < 10^9
```

### Proof of Concept

```rust
#[test]
fn test_add_expiration_u64_max() {
    let mut msg = ConsentMessage::GenericDisplayMessage(String::new());
    msg.add_expiration(Some(u64::MAX), None);
    if let ConsentMessage::GenericDisplayMessage(s) = &msg {
        // Fails today: s contains "Invalid nanosecond: 4294967295"
        assert!(
            !s.contains("Invalid nanosecond"),
            "Consent message shows garbled expiration: {s}"
        );
    }
}
``` [3](#0-2)

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
