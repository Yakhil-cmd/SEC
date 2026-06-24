The code is confirmed. Let me verify the full call path and check for any upstream guards on `expires_at`.

Audit Report

## Title
`add_expiration` signed-cast overflow produces garbled consent-message expiration for `expires_at > i64::MAX` — (`packages/icrc-ledger-types/src/icrc21/responses.rs`)

## Summary

`add_expiration` converts a `u64` nanosecond timestamp to a human-readable date by casting it to `i64` before dividing. For any `expires_at > i64::MAX`, the cast wraps to a negative value, the modulo remainder is also negative, and the subsequent `as u32` wrapping produces a nanosecond value ≥ 10⁹. `replace_nanosecond` rejects it, and the `GenericDisplay` consent message emits `"Invalid nanosecond: 4294967295"` instead of the actual expiration date. The ICRC-21 consent message is the sole user-facing security gate before signing; corrupting it for a valid, attacker-controlled input directly undermines its purpose.

## Finding Description

**Root cause:** In `packages/icrc-ledger-types/src/icrc21/responses.rs` lines 232–233:

```rust
let seconds = (ts as i64) / 10_i64.pow(9);
let nanos   = ((ts as i64) % 10_i64.pow(9)) as u32;
```

Both casts are unsafe for `ts > i64::MAX`.

**Exact arithmetic for `ts = u64::MAX = 18_446_744_073_709_551_615`:**

| Step | Expression | Result |
|---|---|---|
| 1 | `u64::MAX as i64` | `-1` (wrapping) |
| 2 | `(-1_i64) / 1_000_000_000` | `0` |
| 3 | `(-1_i64) % 1_000_000_000` | `-1` (sign follows dividend in Rust) |
| 4 | `(-1_i64) as u32` | `4_294_967_295` (wrapping) |
| 5 | `OffsetDateTime::from_unix_timestamp(0)` | `Ok(epoch)` |
| 6 | `.replace_nanosecond(4_294_967_295)` | `Err(...)` — valid range 0..=999_999_999 |
| 7 | fallback | `"Invalid nanosecond: 4294967295"` |

The same failure occurs for **any** `ts > i64::MAX` (i.e., any timestamp after year 2262): the cast is always negative, the `% 10⁹` remainder is always negative, and the `as u32` wrapping always exceeds the valid nanosecond range.

**Call path — no upstream guard exists:**

1. `icrc21_canister_call_consent_message` (ICP ledger `main.rs:1479`) or the ICRC-1 ledger equivalent decodes `ApproveArgs` from attacker-supplied bytes.
2. `expires_at: Option<u64>` is extracted verbatim and passed to `ConsentMessageBuilder::with_expires_at` (`lib.rs:462–463`).
3. `ConsentMessageBuilder::build` calls `message.add_expiration(self.expires_at, ...)` (`lib.rs:241`).
4. `add_expiration` performs the unsafe cast with no range check.

No validation of `expires_at` against `i64::MAX` exists anywhere in the path.

**Secondary issue (`FieldsDisplay` branch, lines 265–269):** the same `as i64` cast is applied, and the result is stored as `seconds as u64`, silently displaying a wrong (epoch-relative) timestamp without any error string.

## Impact Explanation

The ICRC-21 consent message is the protocol-defined user-facing security gate that wallets must display before a user signs an ICRC-2 `approve` transaction. Corrupting it for a valid, attacker-supplied input means the user cannot determine the actual expiration of the allowance they are about to grant. A user who sees `"Invalid nanosecond: 4294967295"` may interpret the field as a harmless display glitch and approve an allowance that expires in year ~2554 — effectively permanent. This constitutes significant ICRC ledger security impact with concrete user harm, qualifying as **High** under the "Significant ck-token, ledger … security impact with concrete user or protocol harm" criterion.

## Likelihood Explanation

- No privilege is required; `icrc21_canister_call_consent_message` is callable by any principal.
- The trigger value (`expires_at > i64::MAX`) is a valid `u64` accepted by the Candid interface without rejection.
- The path is fully deterministic and locally reproducible with a single unit test.
- A malicious dApp need only encode `ApproveArgs { expires_at: Some(u64::MAX), ... }` and request `GenericDisplay`.

## Recommendation

Replace the unsafe `as i64` cast with safe arithmetic that stays in `u64` until the final conversion:

```rust
let seconds_u64 = ts / 1_000_000_000u64;
let nanos_u32   = (ts % 1_000_000_000u64) as u32; // safe: always < 10^9
let seconds = match i64::try_from(seconds_u64) {
    Ok(s) => s,
    Err(_) => return format!("Invalid timestamp: {ts}"),
};
```

Apply the same fix to the `FieldsDisplay` branch (lines 265–269).

## Proof of Concept

```rust
#[test]
fn test_add_expiration_u64_max() {
    let mut msg = ConsentMessage::GenericDisplayMessage(String::new());
    msg.add_expiration(Some(u64::MAX), None);
    if let ConsentMessage::GenericDisplayMessage(s) = &msg {
        assert!(
            !s.contains("Invalid nanosecond"),
            "Consent message shows garbled expiration: {s}"
        );
    }
}
```

Running this test against the current code at `packages/icrc-ledger-types/src/icrc21/responses.rs` will fail with the message `"Consent message shows garbled expiration: … Invalid nanosecond: 4294967295"`, confirming the bug deterministically without any network access or special privileges.