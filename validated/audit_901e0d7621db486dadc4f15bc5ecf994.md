The code is directly readable and the claim is mechanically verifiable. Let me confirm the exact call chain and the `to_f64` behavior.

### Title
f64 Precision Loss in `convert_tokens_to_string_representation` Causes Consent Message to Display Wrong Token Amount — (`packages/icrc-ledger-types/src/icrc21/responses.rs`)

---

### Summary

`convert_tokens_to_string_representation` converts a `Nat` (arbitrary-precision integer) to `f64` before formatting. IEEE 754 double precision has a 53-bit mantissa, so any integer > 2^53 that is not a power of two cannot be represented exactly. For `amount = 2^53 + 1 = 9007199254740993`, the displayed string is `"9007199254740992"` — one unit less than the actual encoded amount. Because ICRC-21 consent messages are the sole mechanism wallets use to show users what they are about to sign, this creates a verifiable discrepancy between the displayed amount and the amount that will actually be transferred or approved.

---

### Finding Description

The vulnerable function is:

```rust
// packages/icrc-ledger-types/src/icrc21/responses.rs, lines 318-327
fn convert_tokens_to_string_representation(
    tokens: Nat,
    decimals: u8,
) -> Result<String, Icrc21Error> {
    let tokens = tokens.0.to_f64().ok_or(Icrc21Error::GenericError { ... })?;
    Ok(format!("{}", tokens / 10_f64.pow(decimals)))
}
```

`tokens.0` is a `num_bigint::BigUint`. `to_f64()` (from `num_traits::ToPrimitive`) performs a lossy conversion: any integer whose value requires more than 53 significant bits is rounded to the nearest representable `f64`. The result is then formatted and embedded in the `GenericDisplayMessage` string. [1](#0-0) 

This function is called from three places, all on the `GenericDisplayMessage` branch:

- `add_amount` — displays the transfer/approval amount
- `add_allowance` — displays the requested allowance for `icrc2_approve`
- `add_fee` — displays the fee [2](#0-1) [3](#0-2) [4](#0-3) 

The `FieldsDisplayMessage` branch is **not** affected — it uses `nat_to_u64` which is exact (though it rejects values > u64::MAX). [5](#0-4) 

The full call chain from an unprivileged ingress call:

1. Any caller → `icrc21_consent_message` endpoint (public, no auth)
2. → `build_icrc21_consent_info` decodes the `arg` bytes and extracts the `Nat` amount
3. → `ConsentMessageBuilder::build()` → `add_amount` / `add_allowance` / `add_fee`
4. → `convert_tokens_to_string_representation` → `to_f64()` → precision loss [6](#0-5) 

---

### Impact Explanation

The ICRC-21 consent message is the **only** information a wallet presents to the user before they authorize a transaction. If the displayed amount differs from the amount encoded in `arg`, the user signs a transaction for a different value than what they saw. The actual on-chain transfer uses the `arg` bytes directly — the display string has no effect on execution — so the ledger will process the true amount while the user believed they approved a different one.

Concretely, with `decimals = 0` and `amount = 9007199254740993` (2^53 + 1):
- Displayed: `"9007199254740992"`
- Actual transfer: `9007199254740993`

The discrepancy grows for amounts further above 2^53: at 2^54 the ULP is 2, at 2^55 it is 4, etc. A malicious dApp can craft any amount in the range [2^53, 2^128] (Nat is unbounded) to produce a display string that differs from the true amount.

---

### Likelihood Explanation

The trigger requires only a valid `ConsentMessageRequest` with a `Nat` amount whose `BigUint` value exceeds 2^53. This is a standard, unauthenticated public endpoint. No privileged role, key, or governance action is needed. The only practical constraint is that the token's total supply must allow such a balance, which is true for any token with low decimals or high supply. The `GenericDisplayMessage` display type is the default when no `device_spec` is provided, making it the common path.

---

### Recommendation

Replace the lossy `f64` conversion with exact integer arithmetic. The standard approach for fixed-point token display:

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

---

### Proof of Concept

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

The assertion fails because `9007199254740993u64 as f64` rounds to `9007199254740992.0`. The consent message displays the wrong amount.

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

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L141-141)
```rust
                let fee = convert_tokens_to_string_representation(amount, decimals)?;
```

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L185-186)
```rust
            ConsentMessage::GenericDisplayMessage(message) => {
                let amount = convert_tokens_to_string_representation(amount, decimals)?;
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

**File:** packages/icrc-ledger-types/src/icrc21/lib.rs (L324-396)
```rust
pub fn build_icrc21_consent_info(
    consent_msg_request: ConsentMessageRequest,
    caller_principal: Principal,
    ledger_fee: Nat,
    token_symbol: String,
    token_name: String,
    decimals: u8,
    transfer_args: Option<GenericTransferArgs>,
) -> Result<ConsentInfo, Icrc21Error> {
    if consent_msg_request.arg.len() > MAX_CONSENT_MESSAGE_ARG_SIZE_BYTES as usize {
        return Err(Icrc21Error::UnsupportedCanisterCall(ErrorInfo {
            description: format!(
                "The argument size is too large. The maximum allowed size is {MAX_CONSENT_MESSAGE_ARG_SIZE_BYTES} bytes."
            ),
        }));
    }

    // for now, respond in English regardless of what the client requested
    let metadata = ConsentMessageMetadata {
        language: "en".to_string(),
        utc_offset_minutes: consent_msg_request
            .user_preferences
            .metadata
            .utc_offset_minutes,
    };

    let mut display_message_builder =
        ConsentMessageBuilder::new(&consent_msg_request.method, decimals)?
            .with_ledger_fee(ledger_fee.clone())
            .with_token_symbol(token_symbol)
            .with_token_name(token_name);

    if let Some(offset) = consent_msg_request
        .user_preferences
        .metadata
        .utc_offset_minutes
    {
        display_message_builder = display_message_builder.with_utc_offset_minutes(offset);
    }

    if let Some(display_type) = consent_msg_request.user_preferences.device_spec {
        display_message_builder = display_message_builder.with_display_type(display_type);
    }

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
