Let me look at the full `responses.rs` file and the ICRC-1 ledger's token type constraints to complete the analysis.

Now I have everything needed for a complete analysis. Let me trace the full code path and assess the invariant.

---

### Title
GenericDisplay Consent Message Shows Precision-Lossy f64 Amount for Nat > 2^53 — (`packages/icrc-ledger-types/src/icrc21/responses.rs`)

### Summary

`convert_tokens_to_string_representation` converts a `Nat` token amount to `f64` before formatting it for the `GenericDisplayMessage` variant of the ICRC-21 consent message. Because `f64` has only 53 bits of mantissa, any integer amount above 2^53 ≈ 9 × 10^15 is rounded to the nearest representable float. The `FieldsDisplayMessage` path avoids this by using `nat_to_u64`, which preserves the exact integer. An unprivileged caller can craft a `ConsentMessageRequest` that triggers the lossy path, causing the displayed amount to differ from the amount encoded in the transaction bytes.

### Finding Description

The vulnerable function is:

```rust
// packages/icrc-ledger-types/src/icrc21/responses.rs, lines 318-327
fn convert_tokens_to_string_representation(
    tokens: Nat,
    decimals: u8,
) -> Result<String, Icrc21Error> {
    let tokens = tokens.0.to_f64().ok_or(Icrc21Error::GenericError { … })?;
    Ok(format!("{}", tokens / 10_f64.pow(decimals)))
}
``` [1](#0-0) 

It is called from `add_amount` only for the `GenericDisplayMessage` branch:

```rust
// lines 112-114
ConsentMessage::GenericDisplayMessage(message) => {
    let amount = convert_tokens_to_string_representation(amount, decimals)?;
    …
}
``` [2](#0-1) 

The `FieldsDisplayMessage` branch uses the exact-integer path:

```rust
// lines 116-123
ConsentMessage::FieldsDisplayMessage(fields_display) => fields_display.fields.push((
    "Amount".to_string(),
    Value::TokenAmount { decimals, amount: nat_to_u64(amount)?, … },
)),
``` [3](#0-2) 

The public endpoint `icrc21_canister_call_consent_message` is reachable by any unprivileged caller. It decodes the caller-supplied `arg` bytes as a `TransferArg` (whose `amount` field is `Nat`, an arbitrary-precision integer) and passes the amount directly into the builder: [4](#0-3) 

The display type is also caller-controlled via `user_preferences.device_spec`: [5](#0-4) 

### Impact Explanation

The ICRC-21 consent message is the mechanism by which hardware wallets (e.g., Ledger) show users a human-readable description of what they are about to sign. The security invariant is: **the displayed amount must exactly match the amount encoded in the transaction bytes**.

When a malicious dApp crafts a `TransferArg` with `amount = N` where `N > 2^53`, and requests `GenericDisplay`, the consent message shows `round_f64(N) / 10^decimals` while the actual bytes encode `N`. The user approves based on the displayed (rounded) value; the wallet then signs the exact bytes, debiting the exact `N`.

Concrete precision loss for valid `u64` amounts:
- Near `2^53`: ULP = 1 token unit → displayed amount off by 1
- Near `2^63`: ULP = 1024 token units
- Near `u64::MAX` (≈ 1.844 × 10^19): ULP = 2048 token units

For a token with 8 decimals, 2048 token units = 0.00002048 tokens — small in absolute terms. For tokens with 0 or 1 decimals, the discrepancy is 2048 or 205 whole tokens respectively, which can be financially meaningful.

The `FieldsDisplayMessage` path is unaffected because it uses `nat_to_u64` (exact integer).

### Likelihood Explanation

- The endpoint is public and requires no privilege.
- The caller fully controls both the `amount` field and the `device_spec` (display type).
- Any wallet that requests `GenericDisplay` (the default when `device_spec` is `None`) is affected.
- The `build_icrc21_consent_info` function sets `GenericDisplay` as the default when no `device_spec` is provided. [6](#0-5) 

### Recommendation

Replace the `f64` conversion with exact integer arithmetic. Divide the `Nat` by `10^decimals` using big-integer division and format the integer and fractional parts separately, or use a `rust_decimal`/`bigdecimal` crate. The `FieldsDisplayMessage` path already demonstrates the correct approach: keep the amount as a `u64` (or `Nat`) and let the display layer handle formatting.

### Proof of Concept

```rust
// Demonstrates the invariant violation
let amount = Nat::from(u64::MAX); // 18_446_744_073_709_551_615
let decimals: u8 = 8;

// GenericDisplay path (lossy)
let f = amount.0.to_f64().unwrap();          // 18_446_744_073_709_551_616.0 (rounds UP)
let displayed = f / 10_f64.powi(decimals as i32); // "184467440737.09552"

// Exact value
let exact = "184467440737.09551615";

assert_ne!(displayed.to_string(), exact); // PASSES — invariant violated
```

The displayed string `"184467440737.09552"` differs from the exact value `"184467440737.09551615"` by 1 ULP of f64 near 2^64, which is 2048 token units (0.00002048 tokens at 8 decimals). For tokens with fewer decimals the discrepancy is proportionally larger.

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

**File:** packages/icrc-ledger-types/src/icrc21/lib.rs (L364-366)
```rust
    if let Some(display_type) = consent_msg_request.user_preferences.device_spec {
        display_message_builder = display_message_builder.with_display_type(display_type);
    }
```

**File:** packages/icrc-ledger-types/src/icrc21/lib.rs (L368-396)
```rust
    let consent_message = match display_message_builder.function {
        Icrc21Function::Transfer => {
            let TransferArg {
                memo,
                amount,
                from_subaccount,
                to,
                fee,
                created_at_time: _,
            } = Decode!(&consent_msg_request.arg, TransferArg).map_err(|e| {
                Icrc21Error::UnsupportedCanisterCall(ErrorInfo {
                    description: format!("Failed to decode TransferArg: {e}"),
                })
            })?;
            icrc21_check_fee(&fee, &ledger_fee)?;
            let sender = Account {
                owner: caller_principal,
                subaccount: from_subaccount,
            };
            display_message_builder = display_message_builder
                .with_amount(amount)
                .with_receiver_account(AccountOrId::Account(to))
                .with_from_account(AccountOrId::Account(sender));

            if let Some(memo) = memo {
                display_message_builder =
                    display_message_builder.with_memo(GenericMemo::Icrc1Memo(memo.0));
            }
            display_message_builder.build()
```
