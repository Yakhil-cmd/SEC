Audit Report

## Title
f64 Precision Loss in `convert_tokens_to_string_representation` Displays Incorrect Token Amount in ICRC-21 Consent Message — (`packages/icrc-ledger-types/src/icrc21/responses.rs`)

## Summary

`convert_tokens_to_string_representation` converts a `Nat` token amount to `f64` via `to_f64()` before formatting it for display in an ICRC-21 `GenericDisplayMessage`. Because `f64` has only 53 bits of mantissa, any integer value above 2^53 is rounded to the nearest representable float. The consent message therefore displays a value that differs from the actual transfer amount the user is authorizing, breaking the core ICRC-21 security invariant that the displayed amount must exactly match the authorized amount.

## Finding Description

The vulnerable function is confirmed at `packages/icrc-ledger-types/src/icrc21/responses.rs` lines 318–327:

```rust
fn convert_tokens_to_string_representation(
    tokens: Nat,
    decimals: u8,
) -> Result<String, Icrc21Error> {
    let tokens = tokens.0.to_f64().ok_or(Icrc21Error::GenericError { ... })?;
    Ok(format!("{}", tokens / 10_f64.pow(decimals)))
}
```

This function is called from `add_amount` (L113), `add_fee` (L141), `add_allowance` (L186), and `add_existing_allowance` (L212) — exclusively for the `ConsentMessage::GenericDisplayMessage` branch. The `FieldsDisplayMessage` branch uses `nat_to_u64` (L329–334), which preserves exact integer values.

`GenericDisplayMessage` is the **default** variant when `device_spec` is `None`, as confirmed in `packages/icrc-ledger-types/src/icrc21/lib.rs` lines 173–175:

```rust
Some(DisplayMessageType::GenericDisplay) | None => {
    ConsentMessage::GenericDisplayMessage(Default::default())
}
```

The public, unprivileged `#[update]` endpoint `icrc21_canister_call_consent_message` in `rs/ledger_suite/icp/ledger/src/main.rs` (L1478–1481) accepts a `ConsentMessageRequest` from any caller and routes through this builder. A malicious dapp crafts an amount `A` where `A.to_f64()` rounds to `B ≠ A`, calls the endpoint with `device_spec: None` (or `Some(GenericDisplay)`), and the ledger returns a consent message displaying `B`. The dapp then submits the actual transaction with amount `A`. The user authorized `B` but `A` is transferred.

## Impact Explanation

This matches the Medium allowed impact: **moderate user-funds/security impact**. For ICP (8 decimals), the maximum absolute rounding error for u64-range values is 2^(64−52) = 4096 e8s ≈ 0.00004096 ICP — monetarily small at current prices. However, for ICRC tokens with 0 decimals and high per-unit value, a 4096-unit discrepancy can represent meaningful value. More importantly, the ICRC-21 standard exists precisely to guarantee exact display of authorized amounts to protect users from malicious dapps; any discrepancy, however small, breaks this security guarantee in a deterministic and exploitable way. The `Nat` type also accepts values beyond `u64::MAX`, for which `to_f64()` succeeds but rounding error grows unboundedly.

## Likelihood Explanation

The attack requires a malicious dapp — which is exactly the threat model ICRC-21 is designed to defend against. The entrypoint is fully unprivileged, reachable via normal ingress, and `GenericDisplay` is the default path. The precision loss is deterministic: for any amount `A = 2^53 + 1`, `A.to_f64()` always rounds to `2^53`. No special conditions, timing, or victim mistakes are required beyond the user trusting the consent message (which is the entire purpose of ICRC-21).

## Recommendation

Replace the `f64` conversion with exact integer arithmetic using `BigUint` division and modulo, mirroring the pattern in `rs/nervous_system/humanize/src/lib.rs`:

```rust
fn convert_tokens_to_string_representation(
    tokens: Nat,
    decimals: u8,
) -> Result<String, Icrc21Error> {
    let divisor = BigUint::from(10u64).pow(decimals as u32);
    let whole = &tokens.0 / &divisor;
    let frac = &tokens.0 % &divisor;
    if frac.is_zero() {
        Ok(format!("{}", whole))
    } else {
        Ok(format!("{}.{:0>width$}", whole, frac, width = decimals as usize)
            .trim_end_matches('0').to_string())
    }
}
```

This eliminates the `f64` conversion entirely and preserves exact representation for all `Nat` values.

## Proof of Concept

```rust
use candid::Nat;
use num_bigint::BigUint;
use num_traits::ToPrimitive;

let amount = BigUint::from(2u64.pow(53) + 1); // 9007199254740993
let as_f64 = amount.to_f64().unwrap();         // 9007199254740992.0 — rounds DOWN
let displayed = format!("{}", as_f64 / 1.0);   // "9007199254740992"
assert_ne!(displayed, "9007199254740993");      // FAILS: displayed != actual
// User sees 9007199254740992 but authorizes 9007199254740993
```

A unit test calling `convert_tokens_to_string_representation(Nat::from(BigUint::from(2u64.pow(53) + 1u64)), 0)` and asserting the result equals `"9007199254740993"` will reproduce the failure against the current implementation.