### Title
IEEE 754 Precision Loss in ICRC-21 Consent Message Token Amount Display - (File: `packages/icrc-ledger-types/src/icrc21/responses.rs`)

### Summary
The `convert_tokens_to_string_representation` function in the ICRC-21 consent message library converts an arbitrary-precision `Nat` token amount to `f64` (IEEE 754 double-precision float) before formatting it for display. Any token amount exceeding 2^53 loses precision silently, causing the consent message shown to a user on a hardware wallet or signing device to display a different amount than the one actually being transacted.

### Finding Description
In `packages/icrc-ledger-types/src/icrc21/responses.rs`, the function `convert_tokens_to_string_representation` performs a lossy narrowing conversion from `Nat` (arbitrary precision) to `f64`:

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

`f64` has only 53 bits of mantissa precision. Any `Nat` value above 2^53 = 9,007,199,254,740,992 will be silently rounded to the nearest representable double. The function does not return an error for values in the range (2^53, u64::MAX] — `BigUint::to_f64()` succeeds for all finite values, returning a rounded result.

This function is called in the `GenericDisplayMessage` branch of four public methods:

- `add_amount` — transfer amount display
- `add_fee` — fee display
- `add_allowance` — allowance amount display
- `add_existing_allowance` — existing allowance display [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) 

The `icrc21_canister_call_consent_message` endpoint is exposed as a public `#[update]` call on both the ICRC-1 ledger and the ICP ledger: [6](#0-5) [7](#0-6) 

### Impact Explanation
The ICRC-21 consent message is the security-critical user-facing approval mechanism used by hardware wallets (e.g., Ledger) and other signing devices to show users what they are signing before they authorize a transaction. If the displayed amount diverges from the actual on-chain amount due to floating-point rounding, a user can be misled into approving a transaction for a different value than intended.

**Concrete example (8-decimal token like ICP):**
- Actual amount submitted: `9,007,199,254,740,993` e8s (= 90,071,992.54740993 ICP)
- After `to_f64()`: `9,007,199,254,740,992.0` (2^53, the nearest representable double)
- Consent message displays: `90071992.5474099` ICP — off by 0.00000001 ICP (1 e8s)

**Concrete example (18-decimal token like ckETH):**
- Actual amount: `9,007,199,254,740,993` wei (= 9.007199254740993 ckETH)
- After `to_f64()`: `9,007,199,254,740,992.0`
- Consent message displays: `9.007199254740992` ckETH — off by 1 wei

The rounding error grows for amounts further above 2^53. For amounts near 2^64 (u64::MAX), the rounding error can be on the order of thousands of e8s. The actual ledger transaction uses the original `Nat` value with full precision; only the consent message display is wrong.

### Likelihood Explanation
The `icrc21_canister_call_consent_message` endpoint is callable by any unprivileged ingress sender with no authentication requirement. For ICP (8 decimals), the threshold is ~90 million ICP — reachable by large institutional transfers or whale accounts. For tokens with 18 decimals (ckETH, ckUSDC variants), the threshold is only ~9 tokens, making virtually every non-trivial transfer susceptible. The `GenericDisplayMessage` display type is the default when no `device_spec` is specified, meaning most hardware wallet integrations hit this path. [8](#0-7) 

### Recommendation
Replace the lossy `to_f64()` conversion with exact integer arithmetic. Use `BigUint` division and modulo to split the amount into integer and fractional parts, then format them directly as strings. For example:

```rust
fn convert_tokens_to_string_representation(
    tokens: Nat,
    decimals: u8,
) -> Result<String, Icrc21Error> {
    let divisor = num_bigint::BigUint::from(10u64).pow(decimals as u32);
    let integer_part = &tokens.0 / &divisor;
    let fractional_part = &tokens.0 % &divisor;
    if decimals == 0 {
        Ok(integer_part.to_string())
    } else {
        Ok(format!("{}.{:0>width$}", integer_part, fractional_part, width = decimals as usize))
    }
}
```

This avoids any floating-point representation and preserves full precision for all `Nat` values.

### Proof of Concept
Call `icrc21_canister_call_consent_message` on any ICRC-1 ledger with a `GenericDisplay` transfer request where `amount = 9_007_199_254_740_993` (2^53 + 1) and `decimals = 8`. The returned `GenericDisplayMessage` string will contain `90071992.5474099` (rounded down by 1 e8s) instead of the correct `90071992.54740993`. The actual `icrc1_transfer` call with the same amount will transfer the full `9_007_199_254_740_993` e8s — the consent message and the executed transaction disagree on the amount. [1](#0-0) [6](#0-5)

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

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L203-225)
```rust
    pub fn add_existing_allowance(
        &mut self,
        expected_allowance: Nat,
        decimals: u8,
        token_symbol: &String,
    ) -> Result<(), Icrc21Error> {
        match self {
            ConsentMessage::GenericDisplayMessage(message) => {
                let expected_allowance =
                    convert_tokens_to_string_representation(expected_allowance, decimals)?;
                message.push_str(&format!("\n\n**Existing allowance:** `{expected_allowance} {token_symbol}`\nUntil approval, this allowance remains in effect."));
            }
            ConsentMessage::FieldsDisplayMessage(fields_display) => fields_display.fields.push((
                "Existing allowance".to_string(),
                Value::TokenAmount {
                    decimals,
                    amount: nat_to_u64(expected_allowance)?,
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

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L1189-1207)
```rust
#[update]
fn icrc21_canister_call_consent_message(
    consent_msg_request: ConsentMessageRequest,
) -> Result<ConsentInfo, Icrc21Error> {
    let caller_principal = ic_cdk::api::msg_caller();
    let ledger_fee = icrc1_fee();
    let token_symbol = icrc1_symbol();
    let token_name = icrc1_name();
    let decimals = icrc1_decimals();

    build_icrc21_consent_info_for_icrc1_and_icrc2_endpoints(
        consent_msg_request,
        caller_principal,
        ledger_fee,
        token_symbol,
        token_name,
        decimals,
    )
}
```

**File:** rs/ledger_suite/icp/ledger/src/main.rs (L1478-1541)
```rust
#[update]
fn icrc21_canister_call_consent_message(
    consent_msg_request: ConsentMessageRequest,
) -> Result<ConsentInfo, Icrc21Error> {
    let caller_principal = caller();
    let ledger_fee = Nat::from(LEDGER.read().unwrap().transfer_fee.get_e8s());
    let token_symbol = LEDGER.read().unwrap().token_symbol.clone();
    let token_name = LEDGER.read().unwrap().token_name.clone();
    let decimals = ic_ledger_core::tokens::DECIMAL_PLACES as u8;

    if consent_msg_request.method == "transfer" {
        let TransferArgs {
            memo,
            amount,
            fee,
            from_subaccount,
            to,
            created_at_time: _,
        } = Decode!(&consent_msg_request.arg, TransferArgs).map_err(|e| {
            Icrc21Error::UnsupportedCanisterCall(ErrorInfo {
                description: format!("Failed to decode TransferArgs: {e}"),
            })
        })?;
        icrc21_check_fee(&Some(Nat::from(fee)), &ledger_fee)?;
        let from = if caller() == Principal::anonymous() {
            AccountOrId::AccountIdAddress(None)
        } else {
            let account = Account {
                owner: caller(),
                subaccount: from_subaccount.map(|sa| sa.0),
            };
            AccountOrId::AccountIdAddress(Some(AccountIdentifier::from(account).to_hex()))
        };
        let receiver = AccountIdentifier::from_slice(&to).map_err(|e| {
            Icrc21Error::UnsupportedCanisterCall(ErrorInfo {
                description: format!("Failed to parse receiver account id: {e}"),
            })
        })?;
        let args = GenericTransferArgs {
            from,
            receiver: AccountOrId::AccountIdAddress(Some(receiver.to_hex())),
            amount: Nat::from(amount.get_e8s()),
            memo: Some(GenericMemo::IntMemo(memo.0)),
        };
        build_icrc21_consent_info(
            consent_msg_request,
            caller_principal,
            ledger_fee,
            token_symbol,
            token_name,
            decimals,
            Some(args),
        )
    } else {
        build_icrc21_consent_info_for_icrc1_and_icrc2_endpoints(
            consent_msg_request,
            caller_principal,
            ledger_fee,
            token_symbol,
            token_name,
            decimals,
        )
    }
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
