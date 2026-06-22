### Title
ICRC-21 Consent Message `add_expiration` Silently Displays Epoch 0 for `expires_at = u64::MAX` in FieldsDisplay Path — (`packages/icrc-ledger-types/src/icrc21/responses.rs`)

---

### Summary

The `add_expiration` function in the `FieldsDisplayMessage` branch casts `expires_at: u64` to `i64` before dividing by `10^9`. For `expires_at = u64::MAX`, this wrapping cast produces `-1`, integer division yields `0`, and the re-cast to `u64` stores `0`. The resulting `Value::TimestampSeconds { amount: 0 }` represents Unix epoch (Jan 1, 1970). A hardware wallet rendering this field shows an already-expired date, while the actual on-chain approval — which the ledger accepts because `u64::MAX` nanoseconds ≈ year 2554 — is effectively permanent.

---

### Finding Description

**Root cause — `packages/icrc-ledger-types/src/icrc21/responses.rs`, lines 265–269:**

```rust
ConsentMessage::FieldsDisplayMessage(fields_display) => {
    match expires_at {
        Some(expires_at) => {
            let seconds = (expires_at as i64) / 10_i64.pow(9);   // ← wrapping cast
            fields_display.fields.push((
                "Approval expiration".to_string(),
                Value::TimestampSeconds {
                    amount: seconds as u64,                        // ← 0 for u64::MAX
                },
            ))
``` [1](#0-0) 

Arithmetic for `expires_at = u64::MAX = 18446744073709551615`:

| Step | Expression | Result |
|------|-----------|--------|
| 1 | `u64::MAX as i64` | `-1` (defined wrapping cast in Rust) |
| 2 | `-1_i64 / 1_000_000_000_i64` | `0` (rounds toward zero) |
| 3 | `0_i64 as u64` | `0` |

The stored value is `Value::TimestampSeconds { amount: 0 }` — Unix epoch.

**The ledger accepts `expires_at = u64::MAX`:**

The approval core checks `expires_at <= now` and rejects only past timestamps. Since `u64::MAX` nanoseconds ≈ year 2554, it is strictly greater than the current ledger time and the approval is accepted without error. [2](#0-1) 

**The `GenericDisplay` path behaves differently** — it computes `nanos = (u64::MAX as i64) % 10^9 = -1 % 10^9 = -1`, casts to `u32` yielding `4294967295`, and `replace_nanosecond` fails, returning the string `"Invalid nanosecond: 18446744073709551615"`. This is at least a visible anomaly. The `FieldsDisplay` path silently emits epoch 0 with no error. [3](#0-2) 

---

### Impact Explanation

A malicious dApp constructs `ApproveArgs` with a large `amount` and `expires_at = u64::MAX`, then asks the user to sign via a hardware wallet. The hardware wallet calls `icrc21_canister_call_consent_message` requesting `FieldsDisplay`. The returned consent message contains `TimestampSeconds { amount: 0 }`. The hardware wallet renders this as "Thu, 01 Jan 1970 00:00:00 +0000" — an already-expired date. The user concludes the approval is harmless and signs. The on-chain approval is valid until year ~2554; the attacker's spender immediately drains the approved allowance.

The `icrc2_approve` call path passes `expires_at` directly from `ApproveArgs` to the ledger without any upper-bound sanitization: [4](#0-3) 

---

### Likelihood Explanation

- The attack requires only an unprivileged user interacting with a malicious dApp — no privileged access, no key compromise, no consensus attack.
- Hardware wallets that implement `FieldsDisplay` (the structured display path designed specifically for constrained devices) are the primary target; they are the exact devices ICRC-21 was designed to protect.
- The `FieldsDisplay` variant is explicitly defined in the DID and supported by the ledger. [5](#0-4) 

---

### Recommendation

Replace the wrapping `i64` cast with pure `u64` arithmetic in both branches of `add_expiration`:

```rust
// FieldsDisplay branch — fix:
let seconds = expires_at / 1_000_000_000_u64;
fields_display.fields.push((
    "Approval expiration".to_string(),
    Value::TimestampSeconds { amount: seconds },
))

// GenericDisplay branch — fix:
let seconds = expires_at / 1_000_000_000_u64;
let nanos = (expires_at % 1_000_000_000_u64) as u32;
```

Add a differential invariant test asserting that for any `expires_at` value (including `u64::MAX`), `TimestampSeconds.amount == expires_at / 1_000_000_000` computed in `u64` arithmetic.

---

### Proof of Concept

```rust
// Reproduces the bug locally — no canister needed
let expires_at: u64 = u64::MAX;
let seconds_buggy = (expires_at as i64) / 10_i64.pow(9);
let amount_buggy = seconds_buggy as u64;
assert_eq!(amount_buggy, 0); // displays Jan 1 1970

let seconds_correct = expires_at / 1_000_000_000_u64;
assert_eq!(seconds_correct, 18446744073); // year ~2554

// On-chain: ledger accepts u64::MAX because it is > now
// approvals.rs line 247: expires_at.unwrap_or_else(remote_future) <= now
// u64::MAX > current_time → approval stored, valid until year 2554
``` [6](#0-5) [2](#0-1)

### Citations

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L229-244)
```rust
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

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L262-271)
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
```

**File:** rs/ledger_suite/common/ledger_core/src/approvals.rs (L247-249)
```rust
            if expires_at.unwrap_or_else(remote_future) <= now {
                return Err(ApproveError::ExpiredApproval { now });
            }
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L863-871)
```rust
        let tx = Transaction {
            operation: Operation::Approve {
                from: from_account,
                spender: arg.spender,
                amount,
                expected_allowance,
                expires_at: arg.expires_at,
                fee: arg.fee.map(|_| expected_fee_tokens),
            },
```

**File:** rs/ledger_suite/icrc1/ledger/ledger.did (L481-491)
```text
  };
  TimestampSeconds : record {
    amount : nat64
  };
  DurationSeconds : record {
    amount : nat64
  };
  Text : record {
    content : text
  }
};
```
