### Title
`nat_to_u64` Overflow in ICRC-21 `FieldsDisplayMessage` Consent Generation Causes Denial-of-Service for Large Token Amounts — (`packages/icrc-ledger-types/src/icrc21/responses.rs`)

---

### Summary

The ICRC-21 consent message builder silently fails for any token amount exceeding `u64::MAX` when the caller requests a `FieldsDisplayMessage` (structured display) consent message. The root cause is `nat_to_u64`, which attempts to narrow an arbitrary-precision `Nat` into a `u64` and returns a `GenericError` on overflow. For ckETH (18 decimals), the threshold is only ~18.44 ETH — a realistic amount for any non-trivial holder. The endpoint is publicly callable by any unprivileged ingress sender.

---

### Finding Description

`Value::TokenAmount` stores the amount as a `u64`: [1](#0-0) 

The private helper `nat_to_u64` converts the caller-supplied `Nat` amount to `u64`, returning `Err(GenericError)` if the value does not fit: [2](#0-1) 

This helper is called in the `FieldsDisplayMessage` branch of `add_amount`, `add_fee`, `add_allowance`, and `add_existing_allowance`: [3](#0-2) [4](#0-3) [5](#0-4) 

The `ConsentMessageBuilder::build()` propagates these errors upward via `?`: [6](#0-5) 

The ICRC-1 ledger exposes this as a public `#[update]` endpoint: [7](#0-6) 

The ICP ledger exposes the same endpoint: [8](#0-7) 

The `GenericDisplay` branch is unaffected because it uses `convert_tokens_to_string_representation`, which converts to `f64` instead: [9](#0-8) 

---

### Impact Explanation

Any caller requesting `device_spec = FieldsDisplay` with a transfer/approve/transfer_from amount greater than `u64::MAX` (18,446,744,073,709,551,615) receives a `GenericError { error_code: 500, description: "Failed to convert tokens to u64" }` instead of a consent message. For ckETH (18 decimals), `u64::MAX` corresponds to approximately **18.44 ETH** — a threshold easily exceeded by institutional users, DeFi protocols, or any holder with a significant balance. Hardware wallets such as Ledger that exclusively use `FieldsDisplay` cannot display a consent message for such transactions, effectively blocking those users from signing large ckETH transfers or approvals through their hardware wallet.

---

### Likelihood Explanation

The `icrc21_canister_call_consent_message` endpoint is a standard public `#[update]` call requiring no special permissions. Any user can trigger the failure by submitting a `TransferArg`, `ApproveArgs`, or `TransferFromArgs` with `amount > u64::MAX` and `device_spec = Some(FieldsDisplay)`. For ckETH specifically, amounts above ~18.44 ETH are realistic for any non-trivial holder. The Candid interface for `icrc1_transfer` and `icrc2_approve` accepts `amount: nat` (arbitrary precision), so there is no upstream guard preventing large values from reaching the consent message builder.

---

### Recommendation

Replace the `u64` field in `Value::TokenAmount` with a `Nat` (or `u128`) to accommodate the full range of ICRC-1 token amounts, or change `nat_to_u64` to use a wider integer type. At minimum, the `FieldsDisplayMessage` branch should fall back to a string representation (as `GenericDisplayMessage` already does) rather than returning an error when the amount exceeds `u64::MAX`.

---

### Proof of Concept

Call `icrc21_canister_call_consent_message` on any ICRC-1 ledger (e.g., the ckETH ledger) with:

```
method = "icrc1_transfer"
arg = Encode!(TransferArg {
    amount: Nat::from(u64::MAX) + Nat::from(1u64),  // 18_446_744_073_709_551_616 — just above u64::MAX
    to: <any valid account>,
    fee: None, from_subaccount: None, memo: None, created_at_time: None,
})
user_preferences = ConsentMessageSpec {
    device_spec: Some(DisplayMessageType::FieldsDisplay),
    ...
}
```

The call returns:
```
Err(GenericError { error_code: 500, description: "Failed to convert tokens to u64" })
```

The same call with `device_spec = Some(GenericDisplay)` succeeds, confirming the bug is isolated to the `FieldsDisplayMessage` path. [2](#0-1)

### Citations

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L11-17)
```rust
#[derive(CandidType, Deserialize, Eq, PartialEq, Debug, Serialize, Clone)]
pub enum Value {
    TokenAmount {
        decimals: u8,
        amount: u64,
        symbol: String,
    },
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

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L191-199)
```rust
            ConsentMessage::FieldsDisplayMessage(fields_display) => fields_display.fields.push((
                "Requested allowance".to_string(),
                Value::TokenAmount {
                    decimals,
                    amount: nat_to_u64(amount)?,
                    symbol: token_symbol.to_string(),
                },
            )),
        }
```

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L215-222)
```rust
            ConsentMessage::FieldsDisplayMessage(fields_display) => fields_display.fields.push((
                "Existing allowance".to_string(),
                Value::TokenAmount {
                    decimals,
                    amount: nat_to_u64(expected_allowance)?,
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

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L329-334)
```rust
fn nat_to_u64(tokens: Nat) -> Result<u64, Icrc21Error> {
    tokens.0.to_u64().ok_or(Icrc21Error::GenericError {
        error_code: Nat::from(500_u64),
        description: "Failed to convert tokens to u64".to_owned(),
    })
}
```

**File:** packages/icrc-ledger-types/src/icrc21/lib.rs (L205-212)
```rust
                message.add_amount(self.amount, self.decimals, &token_symbol)?;
                message.add_account("To", receiver_account.to_string());
                message.add_fee(
                    Icrc21Function::Transfer,
                    self.ledger_fee,
                    self.decimals,
                    &token_symbol,
                )?;
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
