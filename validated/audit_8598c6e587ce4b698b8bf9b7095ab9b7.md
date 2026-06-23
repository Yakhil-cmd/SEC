### Title
Floating-Point Precision Loss Causes Incorrect Token Amount in ICRC-21 Consent Message GenericDisplay — (`packages/icrc-ledger-types/src/icrc21/responses.rs`)

---

### Summary

The `convert_tokens_to_string_representation` function converts a raw `Nat` token amount to `f64` before formatting it for display in ICRC-21 `GenericDisplayMessage` consent messages. Because `f64` has only 53 bits of mantissa, token amounts whose raw integer representation exceeds 2^53 (≈ 9 × 10^15) are silently rounded, causing the displayed amount to differ from the actual on-chain amount. This is the direct IC analog of the reported `toFixed(2)` rounding issue: a non-zero amount is shown as a different value, potentially misleading users who rely on the consent message to verify what they are signing.

---

### Finding Description

In `packages/icrc-ledger-types/src/icrc21/responses.rs`, the private function `convert_tokens_to_string_representation` (lines 318–327) is called by `add_amount`, `add_fee`, `add_allowance`, and `add_existing_allowance` whenever the display variant is `ConsentMessage::GenericDisplayMessage`.

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
``` [1](#0-0) 

The function converts the raw `Nat` token count to `f64` via `tokens.0.to_f64()`, then divides by `10_f64.pow(decimals)` and formats the result with Rust's default `Display` for `f64`. Because `f64` has a 53-bit significand, any integer larger than 2^53 = 9,007,199,254,740,992 cannot be represented exactly; the conversion silently rounds to the nearest representable value. The formatted string therefore shows a different number than the actual on-chain amount.

This function is invoked for every `GenericDisplayMessage` consent message built by `add_amount`, `add_fee`, `add_allowance`, and `add_existing_allowance`: [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) 

The `FieldsDisplay` path is unaffected because it passes the raw integer directly via `nat_to_u64`: [6](#0-5) 

The `GenericDisplayMessage` path is the default when no `device_spec` is specified in the `ConsentMessageRequest`, making it the most commonly exercised path.

---

### Impact Explanation

The ICRC-21 standard exists specifically so that hardware wallets and constrained signing devices can show users a human-readable description of what they are about to sign. If the displayed amount differs from the actual amount, a user may approve a transaction believing it transfers a different quantity of tokens.

Precision thresholds by token type:
- **18-decimal tokens (e.g., ckETH):** raw amounts above 2^53 ≈ 9,007,199,254,740,992 units correspond to only ~9.007 tokens. Any transfer of more than ~9 ckETH will have its raw integer rounded before display.
- **8-decimal tokens (e.g., ICP, ckBTC):** threshold is ~90,071,992 tokens — large but reachable for institutional or protocol-level transfers.

The rounding error is at most 1 ULP of the f64 representation, but it violates the integrity guarantee of the consent message: the string shown to the user is not the exact amount that will be transferred.

---

### Likelihood Explanation

Any unprivileged caller can invoke `icrc21_canister_call_consent_message` on any ICRC-1/ICRC-2 ledger canister (ICP ledger, ICRC-1 ledger, ckETH ledger, etc.) with a large token amount. The `GenericDisplayMessage` path is the default display mode. The issue is triggered automatically and deterministically for any amount whose raw integer representation exceeds 2^53. No special privileges, keys, or social engineering are required.

---

### Recommendation

Replace the `f64`-based conversion with integer arithmetic. The codebase already contains a correct reference implementation in `rs/bitcoin/ckbtc/minter/src/updates/icrc21.rs`:

```rust
pub(super) fn format_amount(amount: u64, decimals: u8) -> String {
    let divisor = 10_u64.pow(decimals as u32);
    let whole = amount / divisor;
    let frac = amount % divisor;
    if frac == 0 {
        format!("{whole}")
    } else {
        let frac_str = format!("{frac:0width$}", width = decimals as usize);
        let trimmed = frac_str.trim_end_matches('0');
        format!("{whole}.{trimmed}")
    }
}
``` [7](#0-6) 

`convert_tokens_to_string_representation` should be rewritten using the same integer-division approach, operating on the `Nat` value directly (or after a checked conversion to `u128`/`u64`) to avoid any floating-point rounding.

---

### Proof of Concept

**Token:** 18 decimals (e.g., ckETH)
**Amount:** `9_007_199_254_740_993` raw units (= 2^53 + 1 = 9.007199254740993 tokens)

```
tokens.0.to_f64()  →  9_007_199_254_740_992.0   // rounds DOWN by 1 unit
/ 10_f64.pow(18)   →  0.009007199254740992
format!("{}", …)   →  "0.009007199254740992"
```

**Actual amount:** `0.009007199254740993` tokens
**Displayed amount:** `0.009007199254740992` tokens

The consent message shown to the user on their hardware wallet or signing device is incorrect. The user approves a transfer believing it is for `0.009007199254740992` tokens, but the ledger will debit `0.009007199254740993` tokens. This is the same class of misleading-display vulnerability as the reported `toFixed(2)` issue: a non-zero amount is displayed as a different value due to a lossy numeric conversion.

### Citations

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L112-114)
```rust
            ConsentMessage::GenericDisplayMessage(message) => {
                let amount = convert_tokens_to_string_representation(amount, decimals)?;
                message.push_str(&format!("\n\n**Amount:** `{amount} {token_symbol}`"));
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

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L140-141)
```rust
            ConsentMessage::GenericDisplayMessage(message) => {
                let fee = convert_tokens_to_string_representation(amount, decimals)?;
```

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L185-186)
```rust
            ConsentMessage::GenericDisplayMessage(message) => {
                let amount = convert_tokens_to_string_representation(amount, decimals)?;
```

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L211-212)
```rust
                let expected_allowance =
                    convert_tokens_to_string_representation(expected_allowance, decimals)?;
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

**File:** rs/bitcoin/ckbtc/minter/src/updates/icrc21.rs (L211-222)
```rust
pub(super) fn format_amount(amount: u64, decimals: u8) -> String {
    let divisor = 10_u64.pow(decimals as u32);
    let whole = amount / divisor;
    let frac = amount % divisor;
    if frac == 0 {
        format!("{whole}")
    } else {
        let frac_str = format!("{frac:0width$}", width = decimals as usize);
        let trimmed = frac_str.trim_end_matches('0');
        format!("{whole}.{trimmed}")
    }
}
```
