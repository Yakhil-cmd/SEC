Audit Report

## Title
Lossy `f64` Conversion in `convert_tokens_to_string_representation` Causes Silent Amount Truncation in ICRC-21 Consent Messages - (`packages/icrc-ledger-types/src/icrc21/responses.rs`)

## Summary

`convert_tokens_to_string_representation` converts `Nat` token amounts to display strings via a lossy `f64` cast. Because `f64` has only 53 bits of mantissa, any integer amount exceeding 2^53 (≈ 9.007 × 10^15) is silently rounded before display. For tokens with 18 decimals (ckETH, ckERC20), 2^53 units equals only ~0.009 whole tokens, meaning virtually every non-trivial transaction triggers a display discrepancy in the ICRC-21 `GenericDisplayMessage` consent screen — the primary security-critical approval UI shown to users by wallets.

## Finding Description

The vulnerable function is at `packages/icrc-ledger-types/src/icrc21/responses.rs` lines 318–327:

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

`BigUint::to_f64()` returns `None` only when the value overflows `f64::MAX` (~1.8 × 10^308). For any value in the range (2^53, f64::MAX), it returns `Some(rounded_value)`, silently discarding low-order bits. The `ok_or` guard provides no protection against this silent precision loss.

This function is called in the `GenericDisplayMessage` branch of `add_amount` (L113), `add_fee` (L141), `add_allowance` (L186), and `add_existing_allowance` (L212). The `GenericDisplayMessage` path is the default when `device_spec` is `None` (confirmed in `rs/bitcoin/ckbtc/minter/src/updates/icrc21.rs` L89: `unwrap_or(DisplayMessageType::GenericDisplay)`).

The `FieldsDisplayMessage` path is unaffected — it uses `nat_to_u64` (exact integer conversion, L329–334), but this path requires the caller to explicitly request `FieldsDisplay`.

A correct integer-arithmetic approach already exists in the same codebase at `rs/bitcoin/ckbtc/minter/src/updates/icrc21.rs` lines 211–222 (`format_amount`), which uses `u64` division and modulo with no floating-point involved.

## Impact Explanation

This is a concrete display-integrity failure in the ICRC-21 consent message system — the mechanism wallets use to show users exactly what they are authorizing before signing. For 18-decimal tokens (ckETH, ckERC20):

- 2^53 = 9,007,199,254,740,992 units ÷ 10^18 = **~0.009 whole tokens**
- Any transfer or approval of more than ~0.009 tokens triggers rounding
- Example: amount `10,000,000,000,000,001` displays as `10.0` instead of `10.000000000000000001`

A user sees and approves `10.0 TOKEN` in the consent screen, but the on-chain allowance or transfer is `10.000000000000000001 TOKEN`. For `icrc2_approve`, the granted allowance exceeds what the user believed they authorized. This constitutes a **significant ck-token/ledger security impact with concrete user harm**, qualifying as High under the bounty scope.

## Likelihood Explanation

No privilege is required. Any caller can invoke `icrc21_consent_message` on any deployed ICRC-1/ICRC-2 ledger canister. For 18-decimal tokens (ckETH, all ckERC20 tokens), the threshold is ~0.009 whole tokens — triggered by essentially every real-world transaction. The `GenericDisplayMessage` path is the default when no `device_spec` is specified, which is the common case for wallets that do not send a device spec. The issue is deterministic and repeatable.

## Recommendation

Replace the lossy `f64` path with exact `BigUint` integer arithmetic, mirroring the correct approach in `rs/bitcoin/ckbtc/minter/src/updates/icrc21.rs`:

```rust
fn convert_tokens_to_string_representation(
    tokens: Nat,
    decimals: u8,
) -> Result<String, Icrc21Error> {
    use num_bigint::BigUint;
    let divisor = BigUint::from(10_u64).pow(decimals as u32);
    let whole = &tokens.0 / &divisor;
    let frac = &tokens.0 % &divisor;
    if frac.is_zero() {
        Ok(format!("{whole}"))
    } else {
        let frac_str = format!("{frac:0>width$}", width = decimals as usize);
        let trimmed = frac_str.trim_end_matches('0');
        Ok(format!("{whole}.{trimmed}"))
    }
}
```

This eliminates both the `f64` mantissa truncation and the floating-point division rounding, and handles arbitrary-precision `Nat` inputs correctly.

## Proof of Concept

1. Deploy any ICRC-2 ledger canister configured with 18 decimals (e.g., a standard ckETH or ckERC20 ledger).
2. Call `icrc21_consent_message` with method `icrc2_approve`, an `amount` of `10_000_000_000_000_001`, and no `device_spec` (triggering `GenericDisplayMessage`).
3. Observe the returned `GenericDisplayMessage` string contains `**Requested allowance:** \`10 TOKEN\`` instead of `**Requested allowance:** \`10.000000000000000001 TOKEN\``.
4. Alternatively, write a unit test against `convert_tokens_to_string_representation(Nat::from(10_000_000_000_000_001_u64), 18)` and assert the result equals `"10.000000000000000001"` — it will instead return `"10"`.