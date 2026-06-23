### Title
ICRC-21 Consent Message Rendering Invariability Violation via Floating-Point Precision Loss in `GenericDisplayMessage` - (File: `packages/icrc-ledger-types/src/icrc21/responses.rs`)

---

### Summary

The `convert_tokens_to_string_representation` function in the ICRC-21 consent message library converts an arbitrary-precision `Nat` token amount to `f64` before formatting it for display. Because `f64` has only 53 bits of mantissa (~15–16 significant decimal digits), distinct large token amounts that differ only in low-order bits are rounded to the same floating-point value and produce identical rendered strings. This violates the rendering invariability requirement of ICRC-21 consent messages and prevents users from giving informed consent.

---

### Finding Description

In `packages/icrc-ledger-types/src/icrc21/responses.rs`, the private helper `convert_tokens_to_string_representation` is:

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

This function is called by every amount-rendering method on `ConsentMessage` for the `GenericDisplayMessage` path:

- `add_amount` (line 113) [2](#0-1) 
- `add_fee` (line 141) [3](#0-2) 
- `add_allowance` (line 186) [4](#0-3) 
- `add_existing_allowance` (line 212) [5](#0-4) 

The `FieldsDisplay` path is unaffected because it stores the raw integer via `nat_to_u64` and leaves rendering to the wallet. [6](#0-5) 

The `GenericDisplayMessage` path is the default when no `device_spec` is provided, and is the path used by most wallets and signers today. [7](#0-6) 

The `icrc21_consent_message` endpoint is exposed on both the ICP ledger and the ICRC-1 ledger: [8](#0-7) [9](#0-8) 

---

### Impact Explanation

`f64` can represent integers exactly only up to 2^53 = 9,007,199,254,740,992. Any integer above this threshold is rounded to the nearest representable value. For a token with 8 decimals (e.g., ICP), this threshold corresponds to approximately 90,071,992.547 tokens — a realistic balance for large holders, exchanges, or treasury accounts.

Two distinct amounts that differ by 1 e8 above this threshold produce the same rendered string in the consent message. A user shown `"Amount: 90071992.54740992 ICP"` cannot determine whether the actual on-chain amount is `9_007_199_254_740_992` or `9_007_199_254_740_993` e8s. The consent message is not a unique representation of the transaction, violating ICRC-21's rendering invariability requirement and preventing informed consent.

---

### Likelihood Explanation

The `icrc21_consent_message` endpoint is a public query endpoint callable by any unprivileged principal. No special role, key, or governance majority is required. Any user with a balance above ~90 million ICP (or equivalent for other ICRC-1 tokens with 8 decimals) who uses a `GenericDisplay`-mode signer is affected. For tokens with fewer decimals the threshold is proportionally lower. The endpoint is already integrated into hardware wallet flows (e.g., the Ledger ICP app), making this a realistic user-facing risk.

---

### Recommendation

Replace the lossy `f64` conversion in `convert_tokens_to_string_representation` with exact arbitrary-precision integer arithmetic. Divide the `Nat` by `10^decimals` using integer division to obtain the whole part, and compute the fractional part from the remainder, formatting it with the correct number of leading zeros. This is the same approach used by the already-correct `format_amount` helper in the ckBTC minter: [10](#0-9) 

Apply the same pattern to `convert_tokens_to_string_representation` so that every distinct `Nat` amount produces a distinct rendered string.

---

### Proof of Concept

For a token with `decimals = 8` (e.g., ICP), call `icrc21_consent_message` with `GenericDisplay` and two `icrc1_transfer` arguments whose amounts differ by 1 e8 above the `f64` exact-integer threshold:

- **Amount A**: `9_007_199_254_740_992` e8s
- **Amount B**: `9_007_199_254_740_993` e8s

Both amounts pass through `convert_tokens_to_string_representation`:

```
to_f64(9_007_199_254_740_992) = 9007199254740992.0  (exact, = 2^53)
to_f64(9_007_199_254_740_993) = 9007199254740992.0  (rounded down, 2^53+1 is not representable)
```

Both produce `9007199254740992.0 / 1e8 = 90071992.54740992`, so both consent messages display:

```
**Amount:** `90071992.54740992 ICP`
```

A user approving Amount B via a hardware wallet sees the same screen as for Amount A and cannot detect the discrepancy, violating rendering invariability and informed consent.

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

**File:** packages/icrc-ledger-types/src/icrc21/lib.rs (L173-176)
```rust
        let mut message = match self.display_type {
            Some(DisplayMessageType::GenericDisplay) | None => {
                ConsentMessage::GenericDisplayMessage(Default::default())
            }
```

**File:** rs/ledger_suite/icp/ledger/src/main.rs (L1-5)
```rust
#[cfg(feature = "canbench-rs")]
mod canbench;

use candid::Decode;
use candid::{Encode, Nat, Principal, candid_method};
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L1-5)
```rust
#[cfg(feature = "canbench-rs")]
mod benches;

use candid::Principal;
use candid::types::number::Nat;
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
