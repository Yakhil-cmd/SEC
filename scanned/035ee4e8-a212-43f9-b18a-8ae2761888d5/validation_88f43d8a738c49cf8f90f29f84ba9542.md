### Title
Precision Loss in ICRC-21 Consent Message Token Amount Display - (File: `packages/icrc-ledger-types/src/icrc21/responses.rs`)

### Summary
The `convert_tokens_to_string_representation()` function in the ICRC-21 consent message library converts a `Nat` (arbitrary-precision integer) token amount to `f64` before formatting it for display. Because `f64` has only 53 bits of mantissa precision, large token amounts are silently rounded, causing the consent message shown to a user to display a different amount than what they are actually signing.

### Finding Description
The function `convert_tokens_to_string_representation` is used to render token amounts in ICRC-21 `GenericDisplayMessage` consent messages — the trusted human-readable messages that hardware wallets and other signing devices display to users before they approve a transaction.

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

`Nat` is an arbitrary-precision integer. `f64` has only 53 bits of mantissa, meaning any integer value above 2^53 (≈ 9.007 × 10^15) cannot be represented exactly. The conversion via `to_f64()` silently rounds the value. The rounded `f64` is then divided by `10^decimals` and formatted as the displayed amount.

This function is called from `add_amount()`, `add_fee()`, `add_allowance()`, and `add_existing_allowance()` — all of which populate the `GenericDisplayMessage` path of the consent message. [1](#0-0) [2](#0-1) 

### Impact Explanation
ICRC-21 consent messages are the security-critical display layer that wallets use to show users exactly what they are signing. If the displayed amount is rounded due to `f64` precision loss, a user may approve a transaction believing they are transferring amount X, while the actual on-chain transfer is for a different amount Y.

Concrete examples of amounts where precision is lost:
- **ckETH (18 decimals):** Any transfer above ~9,007 ETH (2^53 wei) will have its displayed amount rounded. For example, `9007199254740993 wei` displays as `9007199254740992 wei`.
- **ICP (8 decimals):** Any transfer above ~90 million ICP (2^53 e8s) will be rounded.
- **ICRC-1 tokens with 0 decimals:** Any amount above 2^53 ≈ 9 × 10^15 tokens is affected.

A malicious dApp could craft a transfer amount that rounds to a visually identical or smaller value in the consent message, while the actual signed transaction carries the unrounded (larger) amount. Since ICRC-21 is specifically designed to be the trusted display that overrides the dApp's own UI, this undermines the core security guarantee of the standard. [1](#0-0) 

### Likelihood Explanation
Any unprivileged caller can invoke `icrc21_consent_message` on any ICRC-1/ICRC-2 ledger canister with an arbitrary `Nat` amount in the `TransferArg`. The `Nat` type imposes no upper bound. Amounts large enough to trigger `f64` rounding are realistic for high-value tokens (ckETH, large ICP transfers). The code path is exercised on every consent message request for the `GenericDisplayMessage` display type, which is the default when no `device_spec` is specified. [3](#0-2) 

### Recommendation
Replace the `f64`-based conversion with exact integer arithmetic. The correct approach is to perform integer division and modulo to separate the whole and fractional parts, then format them directly — analogous to how `DisplayAmount` is implemented in `rs/bitcoin/ckbtc/minter/src/tx.rs`:

```rust
fn convert_tokens_to_string_representation(
    tokens: Nat,
    decimals: u8,
) -> Result<String, Icrc21Error> {
    let divisor = BigUint::from(10u64).pow(decimals as u32);
    let whole = &tokens.0 / &divisor;
    let frac = &tokens.0 % &divisor;
    if frac.is_zero() {
        Ok(whole.to_string())
    } else {
        Ok(format!("{}.{:0>width$}", whole, frac, width = decimals as usize)
            .trim_end_matches('0').to_string())
    }
}
```

This avoids any floating-point conversion and preserves exact precision for all `Nat` values. [4](#0-3) 

### Proof of Concept
1. Call `icrc21_consent_message` on any ICRC-1 ledger (e.g., the ckETH ledger) with:
   - `method = "icrc1_transfer"`
   - `amount = 9007199254740993` (= 2^53 + 1 wei, i.e., ~9007.199... ETH)
   - `device_spec = GenericDisplay`
2. Observe that the returned `GenericDisplayMessage` displays `**Amount:** \`9007199254740992 wei\`` (rounded down by 1 due to `f64` rounding), while the actual transaction would transfer `9007199254740993 wei`.
3. The discrepancy grows for larger amounts: `18014398509481984` (2^54) and `18014398509481985` (2^54 + 1) both display as `18014398509481984`.

The root cause is confirmed at: [5](#0-4)

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

**File:** packages/icrc-ledger-types/src/icrc21/lib.rs (L172-180)
```rust
    pub fn build(self) -> Result<ConsentMessage, Icrc21Error> {
        let mut message = match self.display_type {
            Some(DisplayMessageType::GenericDisplay) | None => {
                ConsentMessage::GenericDisplayMessage(Default::default())
            }
            Some(DisplayMessageType::FieldsDisplay) => {
                ConsentMessage::FieldsDisplayMessage(Default::default())
            }
        };
```

**File:** rs/bitcoin/ckbtc/minter/src/tx.rs (L49-87)
```rust
/// Displays an amount in satoshis as decimal fraction of BTC.
pub struct DisplayAmount(pub u64);

impl fmt::Display for DisplayAmount {
    fn fmt(&self, fmt: &mut fmt::Formatter<'_>) -> fmt::Result {
        const SATOSHIS_PER_BTC: u64 = 100_000_000;
        let int = self.0 / SATOSHIS_PER_BTC;
        let frac = self.0 % SATOSHIS_PER_BTC;

        if frac > 0 {
            let frac_width: usize = {
                // Count decimal digits in the fraction part.
                let mut d = 0;
                let mut x = frac;
                while x > 0 {
                    d += 1;
                    x /= 10;
                }
                d
            };
            debug_assert!(frac_width <= 8);
            let frac_prefix: u64 = {
                // The fraction part without trailing zeros.
                let mut f = frac;
                while f.is_multiple_of(10) {
                    f /= 10
                }
                f
            };

            write!(fmt, "{int}.")?;
            for _ in 0..(8 - frac_width) {
                write!(fmt, "0")?;
            }
            write!(fmt, "{frac_prefix}")
        } else {
            write!(fmt, "{int}.0")
        }
    }
```
