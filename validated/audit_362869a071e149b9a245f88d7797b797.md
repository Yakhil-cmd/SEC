Let me look at the `num-bigint` `to_f64()` behavior and check how the ledger's `icrc21_canister_call_consent_message` endpoint is exposed.

### Title
f64 Precision Loss in `convert_tokens_to_string_representation` Silently Displays Rounded Token Amounts in GenericDisplayMessage Consent Messages — (`packages/icrc-ledger-types/src/icrc21/responses.rs`)

---

### Summary

`convert_tokens_to_string_representation` converts an arbitrary-precision `Nat` to `f64` before formatting. For any `Nat` value exceeding 2^53 (the f64 mantissa boundary), `BigUint::to_f64()` returns `Some(rounded_f64)` — not `None` — silently discarding precision. The resulting consent message string displays a rounded amount that differs from the true `Nat` the user is actually authorizing. This path is reachable by any unprivileged caller via the public `icrc21_canister_call_consent_message` endpoint when the display type is `GenericDisplay` (the default).

---

### Finding Description

The vulnerable function is:

```rust
// packages/icrc-ledger-types/src/icrc21/responses.rs, lines 318–327
fn convert_tokens_to_string_representation(
    tokens: Nat,
    decimals: u8,
) -> Result<String, Icrc21Error> {
    let tokens = tokens.0.to_f64().ok_or(Icrc21Error::GenericError {
        error_code: Nat::from(500_u64),
        description: "Failed to convert tokens to u64".to_owned(),
    })?;
    Ok(format!("{}", tokens / 10_f64.pow(decimals)))
}
``` [1](#0-0) 

`tokens.0` is a `BigUint`. The `num_traits::ToPrimitive::to_f64()` implementation for `BigUint` returns `Some(f64)` for all values within f64's representable range — it does **not** return `None` on precision loss. It only returns `None` if the value would overflow to infinity. Therefore the `ok_or(...)` guard provides no protection against silent rounding; it is a dead guard for the precision-loss case.

The `GenericDisplayMessage` variant is the default when `device_spec` is `None` or `Some(GenericDisplay)`:

```rust
// lib.rs lines 173–180
let mut message = match self.display_type {
    Some(DisplayMessageType::GenericDisplay) | None => {
        ConsentMessage::GenericDisplayMessage(Default::default())
    }
    Some(DisplayMessageType::FieldsDisplay) => { ... }
};
``` [2](#0-1) 

`convert_tokens_to_string_representation` is called from `add_amount`, `add_fee`, `add_allowance`, and `add_existing_allowance` — all exclusively for the `GenericDisplayMessage` branch: [3](#0-2) 

By contrast, the `FieldsDisplayMessage` branch uses `nat_to_u64()`, which is exact for values ≤ u64::MAX: [4](#0-3) 

The call chain from the public endpoint is:

`icrc21_canister_call_consent_message` → `build_icrc21_consent_info_for_icrc1_and_icrc2_endpoints` → `build_icrc21_consent_info` → `ConsentMessageBuilder::build` → `message.add_amount(...)` → `convert_tokens_to_string_representation` [5](#0-4) 

The `amount` field in `TransferArg` and `ApproveArgs` is typed as `Nat` (arbitrary precision), so any value can be submitted by an unprivileged caller.

---

### Impact Explanation

The consent message is the security boundary between a user and a transaction they are about to authorize. ICRC-21 exists precisely to guarantee that the displayed message faithfully represents the operation. When `GenericDisplayMessage` is used (the default), amounts above 2^53 base units are silently rounded:

- At exactly 2^53 + 1 (= 9007199254740993), the displayed value is 9007199254740992 — off by 1 base unit.
- At 2^63 (~9.2 × 10^18), the ULP of f64 is 1024, so the displayed value can be off by up to 1024 base units.
- At 2^64 (~1.8 × 10^19), the ULP is 2048 base units.

A malicious dApp can craft an `amount` that rounds **down** in the display (e.g., `2^53 + 1`), causing the user to see a smaller amount than they are actually authorizing. The user approves the consent message believing they are signing for the displayed (lower) amount, while the ledger executes the transaction for the true (higher) `Nat` value.

---

### Likelihood Explanation

- The endpoint is public; no privileged role is required.
- The default display type (`None` → `GenericDisplayMessage`) is the most common path.
- The `Nat` type imposes no upper bound on the submitted amount.
- The rounding is silent — no error is returned, no warning is emitted.
- The practical financial impact per rounding event is small (1–2048 base units depending on magnitude), but the invariant violation is categorical: the consent message is guaranteed to be wrong for any amount above 2^53.

---

### Recommendation

Replace the f64 conversion with exact integer arithmetic. Since `Nat` wraps `BigUint`, use big-integer division and modulo to produce the integer and fractional parts as decimal strings directly:

```rust
fn convert_tokens_to_string_representation(
    tokens: Nat,
    decimals: u8,
) -> Result<String, Icrc21Error> {
    use num_bigint::BigUint;
    let divisor = BigUint::from(10u64).pow(decimals as u32);
    let integer_part = &tokens.0 / &divisor;
    let fractional_part = &tokens.0 % &divisor;
    if decimals == 0 {
        Ok(integer_part.to_string())
    } else {
        Ok(format!(
            "{}.{:0>width$}",
            integer_part,
            fractional_part,
            width = decimals as usize
        ))
    }
}
```

This is exact for all `Nat` values regardless of magnitude.

---

### Proof of Concept

```rust
use candid::Nat;
use num_bigint::BigUint;
use num_traits::ToPrimitive;

fn main() {
    // 2^53 + 1: the first integer f64 cannot represent exactly
    let amount = BigUint::from(1u64 << 53) + BigUint::from(1u64);
    let f = amount.to_f64().unwrap(); // returns Some — no error!
    // f == 9007199254740992.0, not 9007199254740993.0
    assert_ne!(f as u64, (1u64 << 53) + 1); // precision lost silently

    // With 8 decimals (ICP-like), the displayed string is:
    let displayed = format!("{}", f / 1e8_f64);
    // "90071992.54740992" instead of "90071992.54740993"
    println!("Displayed: {displayed}"); // wrong last digit
}
```

Any caller submitting `icrc1_transfer` or `icrc2_approve` with `amount = Nat::from(9007199254740993u64)` and requesting a `GenericDisplay` consent message will receive a message showing `90071992.54740992` instead of `90071992.54740993` (for a token with 8 decimals), violating the exact-display invariant of ICRC-21. [6](#0-5)

### Citations

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L111-115)
```rust
        match self {
            ConsentMessage::GenericDisplayMessage(message) => {
                let amount = convert_tokens_to_string_representation(amount, decimals)?;
                message.push_str(&format!("\n\n**Amount:** `{amount} {token_symbol}`"));
            }
```

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L116-123)
```rust
            ConsentMessage::FieldsDisplayMessage(fields_display) => fields_display.fields.push((
                "Amount".to_string(),
                Value::TokenAmount {
                    decimals,
                    amount: nat_to_u64(amount)?,
                    symbol: token_symbol.to_string(),
                },
            )),
```

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L318-327)
```rust
fn convert_tokens_to_string_representation(
    tokens: Nat,
    decimals: u8,
) -> Result<String, Icrc21Error> {
    let tokens = tokens.0.to_f64().ok_or(Icrc21Error::GenericError {
        error_code: Nat::from(500_u64),
        description: "Failed to convert tokens to u64".to_owned(),
    })?;
    Ok(format!("{}", tokens / 10_f64.pow(decimals)))
}
```

**File:** packages/icrc-ledger-types/src/icrc21/lib.rs (L173-180)
```rust
        let mut message = match self.display_type {
            Some(DisplayMessageType::GenericDisplay) | None => {
                ConsentMessage::GenericDisplayMessage(Default::default())
            }
            Some(DisplayMessageType::FieldsDisplay) => {
                ConsentMessage::FieldsDisplayMessage(Default::default())
            }
        };
```

**File:** packages/icrc-ledger-types/src/icrc21/lib.rs (L305-322)
```rust
pub fn build_icrc21_consent_info_for_icrc1_and_icrc2_endpoints(
    consent_msg_request: ConsentMessageRequest,
    caller_principal: Principal,
    ledger_fee: Nat,
    token_symbol: String,
    token_name: String,
    decimals: u8,
) -> Result<ConsentInfo, Icrc21Error> {
    build_icrc21_consent_info(
        consent_msg_request,
        caller_principal,
        ledger_fee,
        token_symbol,
        token_name,
        decimals,
        None,
    )
}
```
