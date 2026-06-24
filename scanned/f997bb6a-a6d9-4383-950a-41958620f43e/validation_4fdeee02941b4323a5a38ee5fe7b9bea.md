### Title
Floating-Point Precision Loss in ICRC-21 Consent Message Token Amount Display - (`packages/icrc-ledger-types/src/icrc21/responses.rs`)

---

### Summary

`convert_tokens_to_string_representation` in `packages/icrc-ledger-types/src/icrc21/responses.rs` converts token amounts to human-readable strings by casting a `Nat` to `f64` and dividing by `10_f64.pow(decimals)`. Because `f64` has only 53 bits of mantissa, any integer amount exceeding 2^53 (≈ 9.007 × 10^15) is silently rounded, causing the displayed amount in ICRC-21 `GenericDisplayMessage` consent screens to differ from the actual on-chain amount being authorized.

---

### Finding Description

The function at the root of the issue is:

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

This function is called from `add_amount`, `add_fee`, `add_allowance`, and `add_existing_allowance` whenever the consent message variant is `GenericDisplayMessage`: [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) 

The two-step precision loss:

1. **`Nat → f64` cast**: `BigUint::to_f64()` returns `None` only for values that overflow `f64::MAX` (~1.8 × 10^308). For any value in the range (2^53, f64::MAX), it returns `Some(rounded_value)` — silently discarding low-order bits. The integer 2^53 + 1 = 9,007,199,254,740,993 rounds to 9,007,199,254,740,992.

2. **`10_f64.pow(decimals)` division**: Floating-point division introduces additional rounding error on top of the already-imprecise numerator.

The `FieldsDisplayMessage` path is unaffected because it uses `nat_to_u64` (exact integer conversion), but the `GenericDisplayMessage` path — the human-readable text shown to wallet users — is fully exposed. [6](#0-5) 

---

### Impact Explanation

ICRC-21 consent messages are the security-critical user-facing approval screen shown by wallets (e.g., Plug, Stoic, NNS dapp) before a user signs an `icrc1_transfer`, `icrc2_approve`, or `icrc2_transfer_from` call. The `GenericDisplayMessage` string is what the user reads and trusts.

**Concrete example — token with 8 decimals (e.g., ICP, ckBTC):**
- Actual amount: `9,007,199,254,740,993` e8s = `90,071,992.54740993` tokens
- `to_f64()` yields `9,007,199,254,740,992.0` (rounds down by 1 e8s)
- Displayed: `90071992.54740992` — off by `0.00000001` tokens (1 satoshi-equivalent)

**Concrete example — token with 18 decimals (Ethereum-bridged assets):**
- Actual amount: `10,000,000,000,000,001` units = `10.000000000000000001` tokens
- `to_f64()` yields `10,000,000,000,000,000.0`
- Displayed: `10.0` — the fractional unit is completely erased

A user approving an `icrc2_approve` allowance sees `10.0 TOKEN` but the actual on-chain allowance granted is `10.000000000000000001 TOKEN`. The mismatch between the displayed and actual value is the same class of display-layer confusion described in the reference report.

---

### Likelihood Explanation

The entry path requires no privilege: any caller can invoke `icrc21_consent_message` on any deployed ICRC-1/ICRC-2 ledger canister (ICP ledger, ckBTC ledger, ckETH ledger, SNS token ledgers, etc.) with a crafted `amount` field exceeding 2^53 in the smallest denomination. For tokens with 18 decimals, amounts above 9 whole tokens already exceed 2^53 units, making this trivially reachable in normal usage. The `GenericDisplayMessage` path is the default when no `device_spec` is specified. [7](#0-6) 

---

### Recommendation

Replace the lossy `f64` path with exact integer arithmetic, mirroring the correct approach already used in `rs/bitcoin/ckbtc/minter/src/updates/icrc21.rs`:

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
``` [8](#0-7) 

For `convert_tokens_to_string_representation`, since the input is `Nat` (arbitrary precision), use `BigUint` integer division and modulo directly rather than converting to `f64`.

---

### Proof of Concept

1. Deploy any ICRC-1/ICRC-2 ledger canister (e.g., the standard ICRC-1 ledger with 18 decimals).
2. Call `icrc21_consent_message` with method `icrc1_transfer` and an `amount` of `10_000_000_000_000_001` (10 tokens + 1 unit, 18 decimals), requesting `GenericDisplay`.
3. Observe the returned `GenericDisplayMessage` string contains `**Amount:** \`10 TOKEN\`` instead of `**Amount:** \`10.000000000000000001 TOKEN\``.
4. The user sees and approves `10 TOKEN` but the ledger records a transfer of `10.000000000000000001 TOKEN`. [1](#0-0)

### Citations

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L111-114)
```rust
        match self {
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

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L210-212)
```rust
            ConsentMessage::GenericDisplayMessage(message) => {
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

**File:** packages/icrc-ledger-types/src/icrc21/lib.rs (L21-31)
```rust
#[derive(Debug, EnumString, EnumIter, Display)]
pub enum Icrc21Function {
    #[strum(serialize = "icrc1_transfer")]
    Transfer,
    #[strum(serialize = "icrc2_approve")]
    Approve,
    #[strum(serialize = "icrc2_transfer_from")]
    TransferFrom,
    #[strum(serialize = "transfer")]
    GenericTransfer,
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
