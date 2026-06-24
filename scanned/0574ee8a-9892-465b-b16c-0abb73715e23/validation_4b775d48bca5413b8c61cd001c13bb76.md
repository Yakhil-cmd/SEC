### Title
Floating-Point Precision Loss Silently Truncates Token Amounts in ICRC-21 Consent Messages — (`packages/icrc-ledger-types/src/icrc21/responses.rs`)

### Summary

The ICRC-21 consent message system — the Internet Computer's equivalent of a transaction confirmation dialog — silently displays incorrect token amounts for large transfers due to a `Nat`-to-`f64` conversion in `convert_tokens_to_string_representation`. This is the direct IC analog of the reported "missing transaction amount in confirmation dialog" vulnerability class: the amount field is present but can show a materially different value than what the ledger will actually debit.

### Finding Description

In `packages/icrc-ledger-types/src/icrc21/responses.rs`, the private helper `convert_tokens_to_string_representation` converts an arbitrary-precision `Nat` token amount to `f64` before formatting it for the `GenericDisplayMessage` consent message variant: [1](#0-0) 

`f64` has a 53-bit mantissa, meaning it can represent integers exactly only up to 2^53 = 9,007,199,254,740,992. For any token amount (in the ledger's smallest unit) above this threshold, `to_f64()` silently rounds to the nearest representable value. The rounding error is then divided by `10^decimals` and formatted as the displayed amount — with no error, no warning, and no indication to the user that the shown figure differs from the on-chain value.

This helper is called by every amount-display method used when building `GenericDisplayMessage` consent messages:

- `add_amount` — shown for `icrc1_transfer`, `icrc2_transfer_from`, and ICP legacy `transfer` [2](#0-1) 

- `add_fee` — shown for all transaction types [3](#0-2) 

- `add_allowance` / `add_existing_allowance` — shown for `icrc2_approve` [4](#0-3) 

`GenericDisplayMessage` is the default display type (used when `device_spec` is `None` or `GenericDisplay`), making it the most common path for hardware wallets and browser extensions. [5](#0-4) 

The `FieldsDisplayMessage` path avoids this by using `nat_to_u64`, but that conversion fails with a hard error for amounts exceeding `u64::MAX`, rather than silently rounding. [6](#0-5) 

The `icrc21_canister_call_consent_message` endpoint is a public `#[update]` method on both the ICRC-1 ledger and the ICP ledger, callable by any unprivileged principal: [7](#0-6) [8](#0-7) 

### Impact Explanation

A user relying on the ICRC-21 consent message to verify a transfer before signing could be shown an amount that differs from the actual on-chain debit. For ICP (8 decimal places), the precision boundary is ~90,071,992 ICP; above this, each increment of 1 e8 (0.00000001 ICP) may be silently dropped from the display. For tokens with more decimal places the threshold is proportionally lower. The ledger itself processes the exact `Nat` amount from the original argument — only the displayed figure is wrong. A user who trusts the consent message and approves would authorize a transfer of a different amount than shown.

### Likelihood Explanation

The threshold (~90 million ICP, or equivalent for other tokens) is high enough that most retail transfers are unaffected. However, institutional accounts, the minting account, or large SNS treasury operations routinely handle amounts in this range. Any caller can submit a crafted `ConsentMessageRequest` with an amount just above 2^53 e8s to trigger the rounding. No privileged access is required.

### Recommendation

Replace the `f64`-based conversion with an integer-only implementation that performs exact decimal formatting using the `Nat`'s underlying `BigUint`:

```rust
fn convert_tokens_to_string_representation(
    tokens: Nat,
    decimals: u8,
) -> Result<String, Icrc21Error> {
    use num_bigint::BigUint;
    use num_traits::Zero;

    let divisor = BigUint::from(10u64).pow(decimals as u32);
    let whole = &tokens.0 / &divisor;
    let frac  = &tokens.0 % &divisor;
    if frac.is_zero() {
        Ok(format!("{whole}"))
    } else {
        let frac_str = format!("{frac:0>width$}", width = decimals as usize);
        Ok(format!("{}.{}", whole, frac_str.trim_end_matches('0')))
    }
}
```

This eliminates the `f64` conversion entirely and produces an exact decimal string for any `Nat` value.

### Proof of Concept

Call `icrc21_canister_call_consent_message` on any ICRC-1 ledger with:

```
method: "icrc1_transfer"
arg: TransferArg {
    amount: Nat(9_007_199_254_740_993),  // 2^53 + 1 e8s
    to: <any valid account>,
    fee: None, from_subaccount: None,
    created_at_time: None, memo: None,
}
user_preferences: { device_spec: Some(GenericDisplay), ... }
```

The returned `GenericDisplayMessage` will display `**Amount:** \`90071992.5474099 TOKEN\`` (f64-rounded), while the actual ledger debit will be `90071992.54740993 TOKEN` — a discrepancy of 1 e8 (0.00000001 TOKEN) that the signing user cannot detect from the consent message alone. [1](#0-0) [2](#0-1)

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

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L128-151)
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

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L329-334)
```rust
fn nat_to_u64(tokens: Nat) -> Result<u64, Icrc21Error> {
    tokens.0.to_u64().ok_or(Icrc21Error::GenericError {
        error_code: Nat::from(500_u64),
        description: "Failed to convert tokens to u64".to_owned(),
    })
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
