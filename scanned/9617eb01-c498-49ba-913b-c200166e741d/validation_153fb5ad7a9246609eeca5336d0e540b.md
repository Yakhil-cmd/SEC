The key code is confirmed. Let me verify the exact arithmetic and check if there are any guards upstream.

The bug is confirmed in the code. Let me verify the arithmetic and check for any upstream guards on `expires_at`.

The arithmetic is confirmed. Here is the complete assessment:

---

### Title
Silent Integer Overflow in `add_expiration` (FieldsDisplay Branch) Silently Emits Epoch-0 Expiration for `expires_at` Values ≥ `i64::MAX + 1` — (`packages/icrc-ledger-types/src/icrc21/responses.rs`)

### Summary

The `FieldsDisplayMessage` branch of `add_expiration` casts a caller-supplied `u64` nanosecond timestamp to `i64` without overflow checking. For any `expires_at ≥ 9223372036854775808` (i.e., `i64::MAX + 1`), the cast wraps to a negative value. Subsequent integer division by `10^9` and re-cast to `u64` produce a silently wrong `TimestampSeconds` amount — specifically `0` (epoch 1970-01-01) for `u64::MAX`. No error is returned; the consent message is emitted as if it were valid.

### Finding Description

**Entrypoint (unprivileged):**

`icrc21_canister_call_consent_message` is a public `#[update]` endpoint on every ICRC-1/ICRC-2 ledger canister. Any principal, including anonymous, can call it with arbitrary Candid-encoded `ApproveArgs`. [1](#0-0) 

**No guard on `expires_at`:**

The decoded `expires_at: Option<u64>` from `ApproveArgs` is passed directly to `with_expires_at` and then to `add_expiration` with no range check. [2](#0-1) 

**The overflow:**

```
expires_at = u64::MAX = 18446744073709551615
step 1: expires_at as i64  →  -1          (wraps; defined in Rust)
step 2: -1_i64 / 10_i64.pow(9)  →  0     (truncates toward zero)
step 3: 0_i64 as u64  →  0               (epoch 0 = 1970-01-01)
```

The `FieldsDisplayMessage` branch emits `Value::TimestampSeconds { amount: 0 }` with no error: [3](#0-2) 

The `GenericDisplayMessage` branch has the identical arithmetic bug (`(ts as i64) / 10_i64.pow(9)`) but calls `time::OffsetDateTime::from_unix_timestamp(0)`, which succeeds and formats as `"Thu, 01 Jan 1970 00:00:00 +0000"` — also wrong, but at least a recognizable date string rather than a raw `0`. [4](#0-3) 

**Additional overflow shape** — for `expires_at = i64::MAX as u64 + 1 = 9223372036854775808`:
```
as i64  →  i64::MIN = -9223372036854775808
/ 10^9  →  -9223372036
as u64  →  18446744064486375616   (wraps to a huge far-future value)
```
So the bug produces two distinct wrong outputs depending on the input range: epoch 0 near `u64::MAX`, or a spuriously large timestamp near `i64::MAX + 1`.

**No existing test covers this range.** All existing tests use realistic near-present timestamps (e.g., `system_time_to_nanos(env.time()) + 3600s`). [5](#0-4) 

### Impact Explanation

ICRC-21 consent messages are the security boundary between a dApp and a hardware-wallet signer (e.g., Ledger). The wallet displays the structured `FieldsDisplayMessage` fields verbatim and asks the user to confirm. If `Approval expiration` shows `TimestampSeconds { amount: 0 }` (epoch 0, year 1970), the user sees a date in the distant past and may reasonably conclude the approval is already expired and therefore harmless — while the actual on-chain approval carries `expires_at = u64::MAX` (~year 2554), granting the spender an effectively permanent allowance. The ledger itself accepts and stores the correct `u64::MAX` value; only the consent display is wrong.

### Likelihood Explanation

The trigger is a single valid Candid-encoded `ApproveArgs` with `expires_at = Some(u64::MAX)` submitted to the public `icrc21_canister_call_consent_message` endpoint. No privileged role, no key material, and no threshold corruption is required. The value `u64::MAX` is a legal `nat64` in Candid and passes all ledger-side validation. The attack surface is any ICRC-2 ledger deployment that exposes ICRC-21 (all production ICRC-1/ICRC-2 ledgers on the IC). Likelihood is low-to-medium: it requires a malicious dApp to deliberately craft the extreme timestamp and a user who relies on the hardware-wallet consent flow, but the technical barrier is trivial.

### Recommendation

Replace the unchecked `as i64` cast with a checked conversion in both branches of `add_expiration`:

```rust
// Instead of: let seconds = (expires_at as i64) / 10_i64.pow(9);
let seconds = match i64::try_from(expires_at / 1_000_000_000u64) {
    Ok(s) => s,
    Err(_) => return /* emit "Invalid timestamp: {expires_at}" */,
};
```

Dividing first in `u64` space avoids the overflow entirely for all representable nanosecond timestamps, and the `try_from` catches the residual edge case. The `GenericDisplayMessage` branch already has an `Err(_)` fallback path for `from_unix_timestamp`; the `FieldsDisplayMessage` branch needs an equivalent guard.

### Proof of Concept

```rust
// Reproducible with a state-machine test:
let approve_args = ApproveArgs {
    expires_at: Some(u64::MAX),
    // ... other fields ...
};
let request = ConsentMessageRequest {
    method: "icrc2_approve".to_owned(),
    arg: Encode!(&approve_args).unwrap(),
    user_preferences: ConsentMessageSpec {
        metadata: ConsentMessageMetadata { language: "en".to_string(), utc_offset_minutes: None },
        device_spec: Some(DisplayMessageType::FieldsDisplay),
    },
};
let info = icrc21_consent_message(&env, canister_id, Principal::anonymous(), request).unwrap();
// Actual:   TimestampSeconds { amount: 0 }   ← epoch 0, 1970-01-01
// Expected: TimestampSeconds { amount: 18446744073 } or an explicit error
```

The arithmetic is deterministic and local-testable without any mainnet interaction. [6](#0-5)

### Citations

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L1189-1207)
```rust
#[update]
fn icrc21_canister_call_consent_message(
    consent_msg_request: ConsentMessageRequest,
) -> Result<ConsentInfo, Icrc21Error> {
    let caller_principal = ic_cdk::api::msg_caller();
    let ledger_fee = icrc1_fee();
    let token_symbol = icrc1_symbol();
    let token_name = icrc1_name();
    let decimals = icrc1_decimals();

    build_icrc21_consent_info_for_icrc1_and_icrc2_endpoints(
        consent_msg_request,
        caller_principal,
        ledger_fee,
        token_symbol,
        token_name,
        decimals,
    )
}
```

**File:** packages/icrc-ledger-types/src/icrc21/lib.rs (L462-464)
```rust
            if let Some(expires_at) = expires_at {
                display_message_builder = display_message_builder.with_expires_at(expires_at);
            }
```

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

**File:** rs/ledger_suite/tests/sm-tests/src/lib.rs (L3863-3874)
```rust
    let approve_args = ApproveArgs {
        spender: spender_account,
        amount: Nat::from(1_000_000_u32),
        from_subaccount: from_account.subaccount,
        expires_at: Some(
            system_time_to_nanos(env.time()) + Duration::from_secs(3600).as_nanos() as u64,
        ),
        expected_allowance: Some(Nat::from(1_000_000_u32)),
        created_at_time: Some(system_time_to_nanos(env.time())),
        fee: Some(Nat::from(FEE)),
        memo: Some(Memo::from(b"test_bytes".to_vec())),
    };
```
