The code is confirmed. Let me verify the exact lines and arithmetic before rendering a verdict.

All three cited files are confirmed in the repository. The code matches the claim exactly. Here is the verdict:

---

Audit Report

## Title
Silent Integer Wrap in `add_expiration` FieldsDisplay Branch Produces Incorrect Consent-Message Expiration Timestamp — (`packages/icrc-ledger-types/src/icrc21/responses.rs`)

## Summary
The `add_expiration` function in `responses.rs` contains two divergent code paths for `GenericDisplay` and `FieldsDisplay`. The `FieldsDisplay` branch performs an unchecked `u64 → i64` cast followed by an unchecked `i64 → u64` cast, silently wrapping any `expires_at > i64::MAX` into a completely wrong `TimestampSeconds` value. The `GenericDisplay` branch uses the same initial cast but then calls fallible `time` library functions that detect and surface the invalid value as an error string. Because the ICRC-1 ledger accepts any `u64` for `expires_at` without range validation, a malicious dApp can submit `expires_at = u64::MAX` and cause a `FieldsDisplay`-capable wallet to display the approval as expiring at Unix epoch (Jan 1, 1970) while the on-chain approval effectively never expires.

## Finding Description

**Root cause — `FieldsDisplay` branch, lines 264–271:**

```rust
Some(expires_at) => {
    let seconds = (expires_at as i64) / 10_i64.pow(9);   // wraps for expires_at > i64::MAX
    fields_display.fields.push((
        "Approval expiration".to_string(),
        Value::TimestampSeconds {
            amount: seconds as u64,   // wraps again for negative seconds
        },
    ))
}
``` [1](#0-0) 

**Concrete arithmetic for `expires_at = u64::MAX`:**

| Step | Value |
|---|---|
| `u64::MAX as i64` | `-1` (two's-complement wrap) |
| `-1_i64 / 1_000_000_000` | `0` (Rust truncates toward zero) |
| `0_i64 as u64` | `0` |
| Displayed as | `TimestampSeconds { amount: 0 }` = Unix epoch |

**`GenericDisplay` branch for the same input correctly surfaces an error:**

Lines 232–244 apply the same `(ts as i64)` cast but then call `time::OffsetDateTime::from_unix_timestamp(seconds)` and `.replace_nanosecond(nanos)`, both of which are fallible. For `u64::MAX`: `seconds = 0` passes the timestamp check, but `nanos = (-1_i64 % 10^9) as u32 = 4_294_967_295` causes `replace_nanosecond` to return `Err`, yielding the string `"Invalid nanosecond: 4294967295"` instead of a structured value. [2](#0-1) 

**The ledger forwards `expires_at` verbatim without range validation:** [3](#0-2) 

**The only guard in the approvals layer rejects only past expirations:**

```rust
if expires_at.unwrap_or_else(remote_future) <= now {
    return Err(ApproveError::ExpiredApproval { now });
}
``` [4](#0-3) 

`u64::MAX` is far in the future, so this guard does not fire and the approval is stored on-chain with `expires_at = u64::MAX`.

**Exploit path:**
1. Attacker deploys a dApp that constructs an `icrc2_approve` call with `expires_at = u64::MAX` and requests `FieldsDisplay` via `icrc21_canister_call_consent_message`.
2. The wallet calls `add_expiration(Some(u64::MAX), ...)` on a `FieldsDisplayMessage`.
3. The double-wrap produces `Value::TimestampSeconds { amount: 0 }`.
4. The wallet renders the expiration as **Jan 1, 1970**, making the approval appear already-expired or trivially short-lived.
5. The user, believing the approval is harmless, signs.
6. The on-chain approval is stored with `expires_at = u64::MAX`, giving the spender unlimited time to drain the approved allowance.

## Impact Explanation

This is a consent-message display integrity failure in the ICRC-21 system, which is the explicit last line of defense between a user and a malicious dApp. The bug defeats that defense: a user relying on the wallet's structured display to make an informed decision receives a materially false expiration timestamp. The direct consequence is that the user may authorize an allowance they would not have authorized had the correct expiration been shown, leading to potential loss of ledger assets up to the approved amount. This matches **High ($2,000–$10,000): Significant ledger security impact with concrete user harm**.

## Likelihood Explanation

The attack requires no special privileges. The attacker controls the dApp and chooses `expires_at = u64::MAX` deliberately. `FieldsDisplay` is an explicitly supported `ConsentMessage` variant present in the production `.did` files for both the ICP ledger and the ICRC-1 ledger. The arithmetic is deterministic and locally reproducible. The only constraint is that the victim must use a wallet that requests `FieldsDisplay` rather than `GenericDisplay`, which is an explicitly supported and production-deployed path.

## Recommendation

Replace the unsafe cast chain in the `FieldsDisplay` branch with unsigned division, eliminating both wraps:

```rust
Some(expires_at) => {
    let seconds = expires_at / 1_000_000_000_u64;  // no cast needed
    fields_display.fields.push((
        "Approval expiration".to_string(),
        Value::TimestampSeconds { amount: seconds },
    ))
}
```

Optionally, mirror the `GenericDisplay` validity check by verifying `time::OffsetDateTime::from_unix_timestamp(seconds as i64).is_ok()` before pushing the field, to keep both branches consistent in their handling of out-of-range inputs.

## Proof of Concept

```rust
#[test]
fn test_add_expiration_fields_display_wrap() {
    use icrc_ledger_types::icrc21::responses::{ConsentMessage, FieldsDisplay, Value};

    let mut msg = ConsentMessage::FieldsDisplayMessage(FieldsDisplay::default());
    msg.add_expiration(Some(u64::MAX), None);

    if let ConsentMessage::FieldsDisplayMessage(ref fd) = msg {
        let (_, val) = fd.fields.last().unwrap();
        // Current code produces 0 (Unix epoch) due to double wrap.
        // Correct value is u64::MAX / 1_000_000_000 = 18_446_744_073.
        assert_eq!(
            *val,
            Value::TimestampSeconds { amount: u64::MAX / 1_000_000_000 },
            "FieldsDisplay wraps u64::MAX to epoch instead of correct seconds"
        );
    }
}
```

This test fails against the current code. A differential test comparing `GenericDisplay` and `FieldsDisplay` output for `expires_at = u64::MAX` will show the two paths disagree: `GenericDisplay` returns `"Invalid nanosecond: 4294967295"` while `FieldsDisplay` returns `TimestampSeconds { amount: 0 }`.

### Citations

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L232-244)
```rust
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
