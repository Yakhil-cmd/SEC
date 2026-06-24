### Title
Markdown Injection in ICRC-21 `GenericDisplayMessage` via Unescaped `token_name` and `token_symbol` - (File: `packages/icrc-ledger-types/src/icrc21/responses.rs`)

---

### Summary

The ICRC-21 consent message library interpolates the ledger's `token_name` and `token_symbol` directly into a Markdown-formatted `GenericDisplayMessage` string without any sanitization or escaping. Because these values are set by the canister deployer at initialization time, a malicious canister developer can craft a `token_name` or `token_symbol` containing Markdown control characters (newlines, `#`, `**`, backticks) that, when rendered by an ICRC-21-compatible wallet, produce a completely different consent screen than the actual transaction being approved.

---

### Finding Description

The `ConsentMessage::add_intent()` function in `packages/icrc-ledger-types/src/icrc21/responses.rs` builds the heading of the `GenericDisplayMessage` by directly formatting the `token_name` into a Markdown `#` heading:

```rust
// Line 53
message.push_str(&format!("# Send {}", token_name.unwrap()));
// Line 65
message.push_str(&format!("# Spend {}", token_name.unwrap()));
``` [1](#0-0) 

Similarly, `add_amount()`, `add_fee()`, `add_allowance()`, and `add_existing_allowance()` interpolate `token_symbol` inside backtick code spans without escaping:

```rust
// Line 114
message.push_str(&format!("\n\n**Amount:** `{amount} {token_symbol}`"));
// Line 144
message.push_str(&format!("\n\n**Approval fees:** `{fee} {token_symbol}`\n..."));
// Line 188
message.push_str(&format!("\n\n**Requested allowance:** `{amount} {token_symbol}`\n..."));
``` [2](#0-1) [3](#0-2) [4](#0-3) 

These values flow from the ledger's initialization parameters. In the ICRC-1 ledger, `icrc21_canister_call_consent_message` reads them directly from canister state and passes them unsanitized to the builder:

```rust
let token_symbol = icrc1_symbol();
let token_name = icrc1_name();
// ...
build_icrc21_consent_info_for_icrc1_and_icrc2_endpoints(
    consent_msg_request, caller_principal, ledger_fee, token_symbol, token_name, decimals,
)
``` [5](#0-4) 

The same pattern applies to the ICP ledger's `icrc21_canister_call_consent_message`: [6](#0-5) 

The `build_icrc21_consent_info` function in the shared library passes `token_name` and `token_symbol` through to the builder without any Markdown sanitization step: [7](#0-6) 

---

### Impact Explanation

A malicious canister developer deploys an ICRC-1 ledger with a crafted `token_name` such as:

```
Legitimate Token\n\n# You are approving unlimited spending\n\nYou are authorizing another address to withdraw ALL funds from your account.
```

When an ICRC-21-compatible wallet calls `icrc21_canister_call_consent_message` and renders the returned `GenericDisplayMessage` as Markdown, the user sees a completely fabricated consent screen. The actual transaction being signed may be a small transfer, but the displayed message claims something entirely different — causing the user to approve a transaction under false pretenses.

For `token_symbol`, a value containing a backtick (e.g., `` FAKE`\n\n**Amount:** 0 FAKE `` ) breaks out of the code span, allowing injection of fake amount fields that override the real amount displayed.

This is a direct analog to the FilSnap issue: user-controlled data (`ctx.config` there; `token_name`/`token_symbol` here) is interpolated into a Markdown-rendered confirmation dialog without escaping, misleading users into signing inaccurate messages.

---

### Likelihood Explanation

Any principal can deploy a canister on the Internet Computer — no privileged access is required. A malicious actor creates a token with an injected `token_name`, builds a dapp around it, and promotes it to users. When users interact via any ICRC-21-supporting wallet (e.g., hardware wallets using the `GenericDisplay` path), they receive the injected consent message. The `GenericDisplayMessage` variant is explicitly the Markdown-rendered path, making this directly exploitable wherever wallets render it.

---

### Recommendation

1. **Sanitize `token_name` and `token_symbol`** before interpolating them into `GenericDisplayMessage` strings. Strip or escape Markdown control characters (`#`, `*`, `` ` ``, `\n`, `_`, `[`, `]`) from these values before use.
2. **Wrap user-controlled values in code spans** (backtick-delimited) and additionally escape any backtick characters within the value itself, or use double-backtick spans.
3. **Validate `token_name` and `token_symbol` at ledger initialization** to reject values containing Markdown-significant characters, analogous to how `validate_address` is used in the ckBTC/ckDOGE minters to prevent injection before interpolation.
4. Consider using the `FieldsDisplayMessage` variant as the canonical safe path, since it passes structured data rather than a pre-rendered Markdown string, leaving rendering responsibility to the wallet.

---

### Proof of Concept

Deploy an ICRC-1 ledger canister with initialization arguments:

```
token_name = "Safe Token\n\n# SECURITY ALERT: Approve unlimited access\n\nYou are granting this dapp permanent control over your entire wallet balance."
token_symbol = "SAFE"
```

Call `icrc21_canister_call_consent_message` with a `icrc1_transfer` arg for 1 token and `DisplayMessageType::GenericDisplay`. The returned `GenericDisplayMessage` will be:

```markdown
# Safe Token

# SECURITY ALERT: Approve unlimited access

You are granting this dapp permanent control over your entire wallet balance.

**From:**
`<user-account>`

**Amount:** `0.00000001 SAFE`
...
```

A wallet rendering this Markdown shows the injected heading and body text, not the real transfer details. The user sees a fabricated security alert and may reject or approve based on false information — in either case, the displayed consent does not accurately represent the transaction being signed. [8](#0-7) [9](#0-8)

### Citations

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L47-85)
```rust
impl ConsentMessage {
    pub fn add_intent(&mut self, intent: Icrc21Function, token_name: Option<String>) {
        match self {
            ConsentMessage::GenericDisplayMessage(message) => match intent {
                Icrc21Function::Transfer | Icrc21Function::GenericTransfer => {
                    assert!(token_name.is_some());
                    message.push_str(&format!("# Send {}", token_name.unwrap()));
                    message
                        .push_str("\n\nYou are approving a transfer of funds from your account.");
                }
                Icrc21Function::Approve => {
                    message.push_str("# Approve spending");
                    message.push_str(
                            "\n\nYou are authorizing another address to withdraw funds from your account.",
                        );
                }
                Icrc21Function::TransferFrom => {
                    assert!(token_name.is_some());
                    message.push_str(&format!("# Spend {}", token_name.unwrap()));
                    message.push_str(
                        "\n\nYou are approving a transfer of funds from a withdrawal account.",
                    );
                }
            },
            ConsentMessage::FieldsDisplayMessage(fields_display) => match intent {
                Icrc21Function::Transfer | Icrc21Function::GenericTransfer => {
                    assert!(token_name.is_some());
                    fields_display.intent = format!("Send {}", token_name.unwrap());
                }
                Icrc21Function::Approve => {
                    fields_display.intent = "Approve spending".to_string();
                }
                Icrc21Function::TransferFrom => {
                    assert!(token_name.is_some());
                    fields_display.intent = format!("Spend {}", token_name.unwrap());
                }
            },
        }
    }
```

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L112-115)
```rust
            ConsentMessage::GenericDisplayMessage(message) => {
                let amount = convert_tokens_to_string_representation(amount, decimals)?;
                message.push_str(&format!("\n\n**Amount:** `{amount} {token_symbol}`"));
            }
```

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L143-150)
```rust
                    Icrc21Function::Approve => message.push_str(&format!(
                        "\n\n**Approval fees:** `{fee} {token_symbol}`\nCharged for processing the approval."
                    )),
                    Icrc21Function::Transfer
                    | Icrc21Function::TransferFrom
                    | Icrc21Function::GenericTransfer => message.push_str(&format!(
                        "\n\n**Fees:** `{fee} {token_symbol}`\nCharged for processing the transfer."
                    )),
```

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L185-190)
```rust
            ConsentMessage::GenericDisplayMessage(message) => {
                let amount = convert_tokens_to_string_representation(amount, decimals)?;
                message.push_str(&format!(
                            "\n\n**Requested allowance:** `{amount} {token_symbol}`\nThis is the withdrawal limit that will apply upon approval."
                        ));
            }
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L1193-1207)
```rust
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

**File:** rs/ledger_suite/icp/ledger/src/main.rs (L1479-1540)
```rust
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
```

**File:** packages/icrc-ledger-types/src/icrc21/lib.rs (L196-212)
```rust
                let token_name = self.token_name.ok_or(Icrc21Error::GenericError {
                    error_code: Nat::from(500_u64),
                    description: "Token Name must be specified.".to_owned(),
                })?;

                message.add_intent(Icrc21Function::Transfer, Some(token_name));
                if !from_account.is_anonymous() {
                    message.add_account("From", from_account.to_string());
                }
                message.add_amount(self.amount, self.decimals, &token_symbol)?;
                message.add_account("To", receiver_account.to_string());
                message.add_fee(
                    Icrc21Function::Transfer,
                    self.ledger_fee,
                    self.decimals,
                    &token_symbol,
                )?;
```

**File:** packages/icrc-ledger-types/src/icrc21/lib.rs (L324-355)
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

```
