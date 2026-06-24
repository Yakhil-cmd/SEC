### Title
Markdown Injection in ICRC-21 Consent Messages via Unsanitized Memo Field - (`packages/icrc-ledger-types/src/icrc21/responses.rs`)

---

### Summary

The `add_memo` function in the shared ICRC-21 consent-message library directly interpolates a user-controlled, UTF-8-decoded memo byte string into a Markdown-formatted `GenericDisplayMessage` without any sanitization. An attacker can craft a memo whose UTF-8 content contains backtick characters and Markdown syntax to break out of the code-span fence and inject fake transaction fields (e.g., a spoofed `**To:**` address) into the consent message that wallets display to users before they sign.

---

### Finding Description

`add_memo` in `packages/icrc-ledger-types/src/icrc21/responses.rs` handles the `GenericMemo::Icrc1Memo` variant by first attempting UTF-8 decoding and then embedding the result verbatim inside a single-backtick code span:

```rust
let memo_str = match std::str::from_utf8(memo.as_slice()) {
    Ok(valid_str) => valid_str.to_string(),
    Err(_) => hex::encode(memo.as_slice()),
};
match self {
    ConsentMessage::GenericDisplayMessage(message) => {
        message.push_str(&format!("\n\n**Memo:**\n`{memo_str}`"));
    }
    ...
}
``` [1](#0-0) 

A single backtick in `memo_str` terminates the code span early. Everything after it is rendered as raw Markdown. An attacker can therefore inject arbitrary Markdown structure — including bold headers and new field entries — into the consent message.

The memo is sourced directly from the caller-supplied `TransferArgs` decoded inside `build_icrc21_consent_info`:

```rust
if let Some(memo) = self.memo {
    message.add_memo(memo);
}
``` [2](#0-1) 

This function is the shared consent-message builder used by every ICRC-1/ICRC-2 ledger on the IC. [3](#0-2) 

By contrast, the ckBTC minter's `icrc21.rs` explicitly validates the `address` field before interpolation to "rule out Markdown-injection vectors":

```rust
/// Verifies that `address` parses as a valid Bitcoin address … rules
/// out Markdown-injection vectors in the GenericDisplay output (e.g. an
/// "address" that contains newlines or backticks crafted to fake additional
/// fields).
fn validate_address(address: &str, network: Network) -> Result<(), Icrc21Error> {
``` [4](#0-3) 

No equivalent sanitization exists for the memo field in the shared library.

---

### Impact Explanation

A malicious dApp or payment-request generator can craft an ICRC-1 transfer whose memo bytes decode as valid UTF-8 containing a payload such as:

```
` 

**To:**
`attacker_principal_here
```

When a victim's wallet calls `icrc21_canister_call_consent_message` with these transfer arguments, the ledger returns a `GenericDisplayMessage` that renders as:

```
# Send <token>

You are approving a transfer of funds from your account.

**From:**
`victim_account`

**Amount:** `1.00 TOKEN`

**To:**
`legitimate_address`

**Memo:**
` 

**To:**
`attacker_principal_here
```

Wallets that truncate long consent messages, display only the last N lines, or render Markdown without strict field-deduplication will show the injected `**To:**` field. A user who sees only the injected section is misled into believing the transfer destination is `attacker_principal_here` while the actual on-chain destination is `legitimate_address` — or vice versa, depending on the attacker's goal. This directly endangers user funds.

---

### Likelihood Explanation

- The memo field in ICRC-1 transfers is a free-form byte array (`opt blob`) with no character restrictions enforced by the ledger.
- Any unprivileged caller can set an arbitrary memo when constructing a transfer.
- The attack is delivered through a normal payment-request or dApp interaction flow: the attacker provides crafted transaction arguments (e.g., via a QR code, deep link, or dApp call) and the victim's wallet calls the ICRC-21 endpoint on their behalf.
- No special privileges, key material, or governance access are required.
- The attack is silent — the on-chain transaction executes normally; only the displayed consent message is corrupted.

---

### Recommendation

Sanitize `memo_str` before embedding it in the Markdown template. Options include:

1. **Escape backticks**: Replace every `` ` `` in `memo_str` with `` \` `` before interpolation.
2. **Use a fenced code block** (triple backticks) and escape any triple-backtick sequences inside the memo.
3. **Hex-encode unconditionally** for the `GenericDisplayMessage` path, reserving UTF-8 display only for the `FieldsDisplayMessage` path where the value is a typed `Value::Text` and rendering is the wallet's responsibility.

Apply the same fix to `GenericMemo::IntMemo` for consistency, and audit all other `format!` calls in `responses.rs` that embed caller-supplied strings into the Markdown output.

---

### Proof of Concept

Craft a memo whose UTF-8 content is:

```
` \n\n**To:**\n`attacker_address
```

Call `icrc21_canister_call_consent_message` with:

```
method = "icrc1_transfer"
arg    = Encode!(TransferArgs {
    to: legitimate_account,
    amount: Nat::from(1_000_000u64),
    memo: Some(Memo::from(b"` \n\n**To:**\n`attacker_address".to_vec())),
    ...
})
```

The returned `GenericDisplayMessage` will contain the injected `**To:**` field after the real one. Any wallet that renders only the tail of the message, or that naively picks the last occurrence of a bold label, will display `attacker_address` as the destination.

### Citations

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

**File:** packages/icrc-ledger-types/src/icrc21/lib.rs (L290-293)
```rust
        if let Some(memo) = self.memo {
            message.add_memo(memo);
        }

```

**File:** packages/icrc-ledger-types/src/icrc21/lib.rs (L305-322)
```rust
pub fn build_icrc21_consent_info_for_icrc1_and_icrc2_endpoints(
    consent_msg_request: ConsentMessageRequest,
    caller_principal: Principal,
    ledger_fee: Nat,
    token_symbol: String,
    token_name: String,
    decimals: u8,
) -> Result<ConsentInfo, Icrc21Error> {
    build_icrc21_consent_info(
        consent_msg_request,
        caller_principal,
        ledger_fee,
        token_symbol,
        token_name,
        decimals,
        None,
    )
}
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/icrc21.rs (L195-208)
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
```
