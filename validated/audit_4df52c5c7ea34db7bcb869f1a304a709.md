### Title
f64 Precision Loss in ICRC-21 Consent Message Token Amount Display - (`packages/icrc-ledger-types/src/icrc21/responses.rs`)

### Summary
The `convert_tokens_to_string_representation` function converts a `Nat` token amount to `f64` before dividing by `10^decimals` to produce the human-readable amount shown in ICRC-21 consent messages. Because `f64` has only 53 bits of mantissa (~15–16 significant decimal digits), any token amount exceeding `2^53 ≈ 9 × 10^15` is silently rounded, causing the amount displayed in the consent message to differ from the amount that will actually be transferred or approved.

### Finding Description
In `packages/icrc-ledger-types/src/icrc21/responses.rs`, the function `convert_tokens_to_string_representation` is:

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

The `Nat` value (a `BigUint`) is converted to `f64` via `.to_f64()`. The `f64` type has a 53-bit mantissa, meaning it can represent integers exactly only up to `2^53 = 9,007,199,254,740,992`. Any `Nat` value larger than this is silently rounded to the nearest representable `f64`. The rounded value is then divided by `10_f64.pow(decimals)` and formatted as the displayed amount.

This function is called from `add_amount`, `add_fee`, `add_allowance`, and `add_existing_allowance` methods on `ConsentMessage`, all of which feed into the `GenericDisplayMessage` branch of the ICRC-21 consent message. [2](#0-1) 

The ICP ledger's `icrc21_canister_call_consent_message` update endpoint calls `build_icrc21_consent_info`, which internally calls these methods: [3](#0-2) 

### Impact Explanation
The ICRC-21 consent message is the security-critical human-readable description shown to users (e.g., on hardware wallets) before they sign a transaction. If the displayed amount is wrong, a user may unknowingly approve a transfer or allowance for a different amount than what is shown. For ICP with 8 decimal places (`decimals = 8`), the precision boundary is at `9,007,199,254,740,992 e8s ≈ 90,071,992 ICP`. Any transfer or approval above ~90 million ICP will display an incorrectly rounded amount. The total ICP supply is ~500 million ICP, so large neuron disbursements, treasury operations, or high-value approvals are directly affected. The actual on-chain transfer amount is unaffected — only the consent message display is wrong — but this defeats the entire purpose of the consent message as a user-protection mechanism.

### Likelihood Explanation
High. The `icrc21_canister_call_consent_message` endpoint is a publicly callable `#[update]` method on the ICP ledger and all ICRC-1 ledgers. Any unprivileged user can invoke it with any token amount. Large ICP amounts (above ~90M ICP) are realistic in the context of NNS treasury operations, large neuron disbursements, or ICRC-2 approvals for DeFi protocols. No special privileges are required to trigger the incorrect display.

### Recommendation
Replace the `f64`-based conversion with exact integer arithmetic. The correct approach is to perform integer division and modulo to split the amount into whole and fractional parts, as already done correctly in the ckBTC and ckDOGE minter's `format_amount` helper:

```rust
pub(super) fn format_amount(amount: u64, decimals: u8) -> String {
    let divisor = 10_u64.pow(decimals as u32);
    let whole = amount / divisor;
    let frac = amount % divisor;
    ...
}
``` [4](#0-3) 

The `convert_tokens_to_string_representation` function should be rewritten to use `BigUint` integer division and modulo directly, avoiding any `f64` conversion.

### Proof of Concept
Consider a transfer of `10_000_000_000_000_001` e8s (just above `2^53`):

```
tokens.0 = BigUint(10_000_000_000_000_001)
tokens.0.to_f64() = 10_000_000_000_000_000.0  // silently rounded — last digit lost
result = 10_000_000_000_000_000.0 / 10_f64.pow(8)
       = "100000000.0"  // displayed as 100,000,000 ICP
```

The actual amount is `100,000,000.00000001 ICP`, but the consent message shows `100000000` ICP. A user approving this via a hardware wallet sees the wrong amount. The discrepancy grows for amounts further above `2^53`.

The entry path is:
1. Unprivileged caller sends an ingress message to `icrc21_canister_call_consent_message` on the ICP ledger with a `transfer` or `icrc1_transfer` argument containing a large amount.
2. The ledger calls `build_icrc21_consent_info` → `add_amount` → `convert_tokens_to_string_representation`.
3. The `Nat → f64` conversion silently rounds the amount.
4. The returned `ConsentMessage::GenericDisplayMessage` contains the wrong amount string. [1](#0-0) [5](#0-4)

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
