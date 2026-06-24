The code is confirmed as described. Let me verify the key facts:

1. `convert_tokens_to_string_representation` at lines 318-327 uses `to_f64()` — lossy conversion confirmed.
2. `GenericDisplayMessage` is the default when no `device_spec` is provided (line 174: `Some(DisplayMessageType::GenericDisplay) | None`).
3. All three callers (`add_amount` L113, `add_fee` L141, `add_allowance` L186) route through this function for the `GenericDisplayMessage` branch.
4. The `FieldsDisplayMessage` branch uses `nat_to_u64` (exact, but rejects > u64::MAX).

The bug is real and mechanically verifiable. The practical constraint is that the user must hold > 2^53 base units of a given token. For ICP (8 decimals), 2^53 / 10^8 ≈ 90 trillion ICP — far exceeding total supply. For tokens with 0 decimals and high supply (e.g., meme tokens), this threshold is reachable. The discrepancy at the minimum exploitable value (2^53+1) is 1 unit, growing to 2048 units at 2^64. The consent message is the sole trust anchor wallets use before signing — a display discrepancy directly undermines that mechanism.

---

Audit Report

## Title
f64 Precision Loss in `convert_tokens_to_string_representation` Causes Consent Message to Display Wrong Token Amount — (`packages/icrc-ledger-types/src/icrc21/responses.rs`)

## Summary
`convert_tokens_to_string_representation` converts a `Nat` (arbitrary-precision integer) to `f64` via `to_f64()` before formatting. IEEE 754 double precision has a 53-bit mantissa, so any integer above 2^53 that is not a power of two is rounded to the nearest representable value. The resulting string is embedded in the ICRC-21 `GenericDisplayMessage`, which is the default consent message type and the sole display mechanism wallets use before a user authorizes a transaction. A malicious dApp can craft a `Nat` amount above 2^53 to produce a consent message that displays a different value than the amount actually encoded in the transaction argument.

## Finding Description
The vulnerable function is at `packages/icrc-ledger-types/src/icrc21/responses.rs` lines 318–327:

```rust
fn convert_tokens_to_string_representation(
    tokens: Nat,
    decimals: u8,
) -> Result<String, Icrc21Error> {
    let tokens = tokens.0.to_f64().ok_or(Icrc21Error::GenericError { ... })?;
    Ok(format!("{}", tokens / 10_f64.pow(decimals)))
}
```

`tokens.0` is a `num_bigint::BigUint`. `to_f64()` (from `num_traits::ToPrimitive`) performs a lossy conversion: any integer requiring more than 53 significant bits is rounded to the nearest representable `f64`. The rounded value is then formatted and embedded in the consent message string.

This function is called from three places, all on the `GenericDisplayMessage` branch:
- `add_amount` (L113) — transfer/approval amount
- `add_fee` (L141) — transaction fee
- `add_allowance` (L186) — requested allowance for `icrc2_approve`

`GenericDisplayMessage` is the default path: `build()` at L173–176 selects it when `display_type` is `None` (i.e., when the caller omits `device_spec`). The `FieldsDisplayMessage` branch uses `nat_to_u64` (exact, but errors on values > u64::MAX) and is unaffected.

The full call chain: any caller → `icrc21_consent_message` (public, unauthenticated) → `build_icrc21_consent_info` → `ConsentMessageBuilder::build()` → `add_amount`/`add_allowance`/`add_fee` → `convert_tokens_to_string_representation` → `to_f64()` → precision loss.

No existing guard checks whether the `Nat` value exceeds 2^53 before the conversion. The `ok_or` only catches the case where `to_f64()` returns `None` (which only occurs for values too large for any `f64`, i.e., above ~1.8×10^308) — it does not detect silent rounding within the representable range.

## Impact Explanation
The ICRC-21 consent message is the only information a wallet presents to the user before they authorize a transaction. If the displayed amount differs from the amount encoded in `arg`, the user signs a transaction for a value different from what they saw. The actual on-chain transfer uses the `arg` bytes directly; the display string has no effect on execution. A malicious dApp can craft any amount in the range [2^53, ~2^1024] to produce a consent message that differs from the true amount. This constitutes a moderate user-funds/security impact on the ICRC ledger consent infrastructure, matching the Medium bounty tier: the attack is constrained by the requirement that the target user hold more than 2^53 base units of the token, but the mechanism is a direct, concrete discrepancy between what the user authorizes and what the ledger executes.

## Likelihood Explanation
Triggering the bug requires only a valid `ConsentMessageRequest` with a `Nat` amount whose `BigUint` value exceeds 2^53. The `icrc21_consent_message` endpoint is public and unauthenticated. No privileged role, key, or governance action is needed. The practical constraint is that the token's total supply and the target user's balance must allow such a value. For ICP (8 decimals), 2^53 base units ≈ 90 trillion ICP, which exceeds total supply. For tokens with 0 or few decimals and high supply (e.g., meme tokens or custom ICRC-1 tokens), the threshold is reachable. The `GenericDisplayMessage` path is the default, making it the common path for any wallet that does not explicitly request `FieldsDisplay`.

## Recommendation
Replace the lossy `f64` conversion with exact integer arithmetic using `num_bigint::BigUint` division and modulo:

```rust
fn convert_tokens_to_string_representation(
    tokens: Nat,
    decimals: u8,
) -> Result<String, Icrc21Error> {
    let divisor = num_bigint::BigUint::from(10u64).pow(decimals as u32);
    let whole = &tokens.0 / &divisor;
    let remainder = &tokens.0 % &divisor;
    if decimals == 0 || remainder.is_zero() {
        Ok(whole.to_string())
    } else {
        Ok(format!("{}.{:0>width$}", whole, remainder, width = decimals as usize))
    }
}
```

This preserves full precision for arbitrarily large `Nat` values and produces a correctly rounded decimal string.

## Proof of Concept

```rust
#[test]
fn test_f64_precision_loss() {
    use candid::Nat;
    use num_bigint::BigUint;

    // 2^53 + 1: first integer not exactly representable as f64
    let amount = Nat(BigUint::from(9007199254740993u64));
    let result = convert_tokens_to_string_representation(amount, 0).unwrap();
    // Fails: result is "9007199254740992", not "9007199254740993"
    assert_eq!(result, "9007199254740993");
}
```

The assertion fails because `9007199254740993u64 as f64` rounds to `9007199254740992.0`. The consent message displays the wrong amount. This test can be added directly to `packages/icrc-ledger-types/src/icrc21/responses.rs` and run with `cargo test`.