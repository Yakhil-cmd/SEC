The code is confirmed. Let me verify the exact arithmetic and the `remote_future` guard to complete the analysis.

### Title
ICRC-21 `add_expiration` Silently Truncates `expires_at > i64::MAX` to Epoch 0 in `FieldsDisplayMessage`, Enabling Hardware-Wallet Deception — (`packages/icrc-ledger-types/src/icrc21/responses.rs`)

---

### Summary

`add_expiration` casts the `u64` nanosecond timestamp to `i64` before dividing by 10⁹. For any `expires_at > i64::MAX` (including `u64::MAX`), the cast wraps to a negative value; integer division toward zero then yields `0`; the final `as u64` cast produces `0`. The `FieldsDisplayMessage` variant stores `Value::TimestampSeconds { amount: 0 }` — Unix epoch (Jan 1 1970) — while the on-chain approval stores the original, far-future timestamp unchanged.

---

### Finding Description

The defective code is in the `FieldsDisplayMessage` arm of `add_expiration`: [1](#0-0) 

```
expires_at = u64::MAX  →  as i64  →  -1
-1 / 1_000_000_000     →  0   (Rust truncates toward zero)
0  as u64              →  0
```

The same cast appears in the `GenericDisplayMessage` arm: [2](#0-1) 

but there the subsequent `replace_nanosecond(4294967295)` call fails and returns the string `"Invalid nanosecond: 4294967295"` — visibly wrong, not silently wrong.

The `FieldsDisplayMessage` arm computes only `seconds` and stores it directly, so the silent corruption is exclusive to that path.

The ledger's `approve` guard only rejects `expires_at <= now`: [3](#0-2) 

`u64::MAX` (and any value > `i64::MAX`, i.e., any timestamp after year 2262) always passes this check, so the ledger accepts and stores the original value while the consent message displays epoch 0.

The `ApproveArgs` type places no upper bound on `expires_at`: [4](#0-3) 

---

### Impact Explanation

A malicious dApp constructs an `icrc2_approve` call with `expires_at = u64::MAX` and invokes `icrc21_canister_call_consent_message` (a query, no privilege required). The ledger returns a `FieldsDisplayMessage` containing `TimestampSeconds { amount: 0 }`. A hardware wallet (Ledger, Keystone, etc.) that implements the ICRC-21 `FieldsDisplay` spec renders this as "Approval expiration: Jan 1 1970 00:00:00 UTC" — an already-elapsed date. A user who interprets this as "the approval expires immediately / is harmless" signs the transaction. The on-chain approval carries `expires_at = u64::MAX` (year 2554), giving the spender a permanent allowance to drain the account.

---

### Likelihood Explanation

- The entry point is a public query call — no privilege, no key, no governance majority required.
- Any dApp can present this crafted consent message to a hardware-wallet user.
- The `FieldsDisplay` path is specifically designed for constrained hardware wallets that cannot parse free-form text, making the silent numeric corruption harder for the device to detect.
- The only friction is that a user must choose to sign despite seeing a 1970 date; some users will interpret this as a UI glitch and proceed.

---

### Recommendation

Replace the lossy `as i64` cast with a checked conversion. Use `u64::checked_div` directly, or saturate/clamp before converting:

```rust
// Correct: pure u64 arithmetic, no cast needed
let seconds = expires_at / 1_000_000_000_u64;
fields_display.fields.push((
    "Approval expiration".to_string(),
    Value::TimestampSeconds { amount: seconds },
));
```

Apply the same fix to the `GenericDisplayMessage` arm (lines 232–233) to eliminate the "Invalid nanosecond" error for large timestamps.

---

### Proof of Concept

```rust
// Differential invariant test (no runtime required)
let expires_at: u64 = u64::MAX;

// Buggy path (current code)
let seconds_buggy = (expires_at as i64) / 10_i64.pow(9); // = 0
let displayed = seconds_buggy as u64;                     // = 0
assert_eq!(displayed, 0); // epoch 0 — WRONG

// Correct path
let seconds_correct = expires_at / 1_000_000_000_u64;    // = 18446744073
assert_ne!(displayed, seconds_correct);                   // invariant violated
```

The on-chain approval stores `expires_at = u64::MAX`; the consent message displays `TimestampSeconds { amount: 0 }`. The invariant `displayed_seconds == expires_at / 1_000_000_000` is broken for all `expires_at > i64::MAX`.

### Citations

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L232-233)
```rust
                        let seconds = (ts as i64) / 10_i64.pow(9);
                        let nanos = ((ts as i64) % 10_i64.pow(9)) as u32;
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

**File:** packages/icrc-ledger-types/src/icrc2/approve.rs (L20-20)
```rust
    pub expires_at: Option<u64>,
```
