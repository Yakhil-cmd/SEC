### Title
Scientific-Notation / Precision-Loss in ICRC-21 Consent Message Amount Display - (`File: packages/icrc-ledger-types/src/icrc21/responses.rs`)

---

### Summary

`convert_tokens_to_string_representation` converts an arbitrary-precision `Nat` token amount to `f64` before formatting it into the ICRC-21 consent message string. For large token amounts (above 2^53 ≈ 9 × 10^15), the `f64` representation silently loses precision, and for extreme values Rust's `f64` Display emits scientific-notation strings (e.g. `1e18`). The consent message shown to the user therefore displays a **different number** than the one actually being transferred or approved.

---

### Finding Description

The function at issue:

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

`BigUint::to_f64()` succeeds (returns `Some`) for any value up to ~1.8 × 10^308, but `f64` has only 53 bits of mantissa (~15–16 significant decimal digits). Any `Nat` larger than 2^53 is rounded to the nearest representable `f64`. The rounded value is then divided by `10^decimals` and formatted with `format!("{}", ...)`.

This function is called from `add_amount`, `add_fee`, `add_allowance`, and `add_existing_allowance` — all of which populate the `GenericDisplayMessage` branch of the consent message: [2](#0-1) [3](#0-2) [4](#0-3) 

---

### Impact Explanation

An unprivileged caller submits an `icrc21_canister_call_consent_message` request to any ICRC-1 ledger canister (ICP ledger, ckBTC, ckETH, SNS ledgers, etc.) with a `transfer` or `approve` argument whose `amount` field is a large `Nat`. The consent message returned to the wallet/signer will display a **rounded or scientifically-notated** amount string instead of the exact value. A user who relies on the consent message to verify the transaction amount before signing will be misled.

Concrete example with `decimals = 8`:
- Actual raw amount: `10_000_000_000_000_001` (10^16 + 1 e8s)
- `to_f64()` → `10000000000000000.0` (last digit silently dropped)
- After dividing by 10^8: `100000000.0`
- Displayed: `"100000000"` — the fractional part `0.00000001` is invisible

For tokens with 18 decimals (e.g. ckETH-style), the precision loss is even more severe: amounts differing by up to 2^(exponent−52) raw units display identically.

**Impact: 3/5** — Consent message integrity is broken for large amounts; users can be tricked into approving a different amount than shown. Does not directly steal funds but undermines the security guarantee of the ICRC-21 consent flow.

---

### Likelihood Explanation

Any unprivileged user can call `icrc21_canister_call_consent_message` with a crafted large `amount`. No special role or key is required. Tokens with 18 decimals (ckETH-style) or high-value balances routinely exceed 2^53 raw units. **Likelihood: 3/5.**

---

### Recommendation

Replace the lossy `f64` conversion with exact integer arithmetic. The correct approach is to perform integer division and modulo on the `BigUint` directly:

```rust
fn convert_tokens_to_string_representation(
    tokens: Nat,
    decimals: u8,
) -> Result<String, Icrc21Error> {
    let divisor = BigUint::from(10u64).pow(decimals as u32);
    let whole = &tokens.0 / &divisor;
    let frac  = &tokens.0 % &divisor;
    if frac.is_zero() {
        Ok(whole.to_string())
    } else {
        Ok(format!("{}.{:0>width$}", whole, frac, width = decimals as usize))
    }
}
```

This mirrors the exact-integer approach already used in `rs/bitcoin/ckbtc/minter/src/updates/icrc21.rs` (`format_amount`) and `rs/dogecoin/ckdoge/minter/src/updates/icrc21.rs` (`format_amount`): [5](#0-4) 

---

### Proof of Concept

Call `icrc21_canister_call_consent_message` on any deployed ICRC-1 ledger with:

```
method = "icrc1_transfer"
arg    = Candid-encoded TransferArgs {
    to:     <any valid account>,
    amount: 10_000_000_000_000_001   // 10^16 + 1 raw units
    fee:    None,
    ...
}
user_preferences.device_spec = GenericDisplay
```

The returned `ConsentMessage::GenericDisplayMessage` will contain:

```
**Amount:** `100000000 <SYMBOL>`
```

instead of the correct:

```
**Amount:** `100000000.00000001 <SYMBOL>`
```

The one-e8s discrepancy is invisible to the user. For tokens with 18 decimals and amounts above 2^53, the error can be orders of magnitude larger.

### Citations

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L101-126)
```rust
    pub fn add_amount(
        &mut self,
        amount: Option<Nat>,
        decimals: u8,
        token_symbol: &String,
    ) -> Result<(), Icrc21Error> {
        let amount = amount.ok_or(Icrc21Error::GenericError {
            error_code: Nat::from(500_u64),
            description: "Amount has to be specified.".to_owned(),
        })?;
        match self {
            ConsentMessage::GenericDisplayMessage(message) => {
                let amount = convert_tokens_to_string_representation(amount, decimals)?;
                message.push_str(&format!("\n\n**Amount:** `{amount} {token_symbol}`"));
            }
            ConsentMessage::FieldsDisplayMessage(fields_display) => fields_display.fields.push((
                "Amount".to_string(),
                Value::TokenAmount {
                    decimals,
                    amount: nat_to_u64(amount)?,
                    symbol: token_symbol.to_string(),
                },
            )),
        }
        Ok(())
    }
```

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L128-172)
```rust
    pub fn add_fee(
        &mut self,
        intent: Icrc21Function,
        amount: Option<Nat>,
        decimals: u8,
        token_symbol: &String,
    ) -> Result<(), Icrc21Error> {
        let amount = amount.ok_or(Icrc21Error::GenericError {
            error_code: Nat::from(500_u64),
            description: "Amount has to be specified.".to_owned(),
        })?;
        match self {
            ConsentMessage::GenericDisplayMessage(message) => {
                let fee = convert_tokens_to_string_representation(amount, decimals)?;
                match intent {
                    Icrc21Function::Approve => message.push_str(&format!(
                        "\n\n**Approval fees:** `{fee} {token_symbol}`\nCharged for processing the approval."
                    )),
                    Icrc21Function::Transfer
                    | Icrc21Function::TransferFrom
                    | Icrc21Function::GenericTransfer => message.push_str(&format!(
                        "\n\n**Fees:** `{fee} {token_symbol}`\nCharged for processing the transfer."
                    )),
                };
            }
            ConsentMessage::FieldsDisplayMessage(fields_display) => {
                let token_amount = Value::TokenAmount {
                    decimals,
                    amount: nat_to_u64(amount)?,
                    symbol: token_symbol.to_string(),
                };
                match intent {
                    Icrc21Function::Approve => fields_display
                        .fields
                        .push(("Approval fees".to_string(), token_amount)),
                    Icrc21Function::Transfer
                    | Icrc21Function::TransferFrom
                    | Icrc21Function::GenericTransfer => fields_display
                        .fields
                        .push(("Fees".to_string(), token_amount)),
                };
            }
        }
        Ok(())
    }
```

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L174-201)
```rust
    pub fn add_allowance(
        &mut self,
        amount: Option<Nat>,
        decimals: u8,
        token_symbol: &String,
    ) -> Result<(), Icrc21Error> {
        let amount = amount.ok_or(Icrc21Error::GenericError {
            error_code: Nat::from(500_u64),
            description: "Amount has to be specified.".to_owned(),
        })?;
        match self {
            ConsentMessage::GenericDisplayMessage(message) => {
                let amount = convert_tokens_to_string_representation(amount, decimals)?;
                message.push_str(&format!(
                            "\n\n**Requested allowance:** `{amount} {token_symbol}`\nThis is the withdrawal limit that will apply upon approval."
                        ));
            }
            ConsentMessage::FieldsDisplayMessage(fields_display) => fields_display.fields.push((
                "Requested allowance".to_string(),
                Value::TokenAmount {
                    decimals,
                    amount: nat_to_u64(amount)?,
                    symbol: token_symbol.to_string(),
                },
            )),
        }
        Ok(())
    }
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
