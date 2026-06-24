Now I have all the code I need. Let me analyze the exact mechanics of the vulnerability.

### Title
Unsanitized Memo Bytes Enable Markdown Injection into ICRC-21 `GenericDisplayMessage`, Allowing Fake Field Spoofing — (`packages/icrc-ledger-types/src/icrc21/responses.rs`)

---

### Summary

The `add_memo` function interpolates user-controlled memo bytes directly into a CommonMark markdown string using a single-backtick code span. Because backtick characters inside the memo are not escaped, an attacker can close the code span early and inject arbitrary markdown — including fake bold field labels and fake code-span addresses — into the `GenericDisplayMessage` returned by `icrc21_canister_call_consent_message`. This undermines the core security guarantee of ICRC-21, which is to give wallets a tamper-proof, ledger-generated description of what the user is actually signing.

---

### Finding Description

**Vulnerable code — `add_memo`, `GenericDisplayMessage` branch:** [1](#0-0) 

```rust
pub fn add_memo(&mut self, memo: GenericMemo) {
    match memo {
        GenericMemo::Icrc1Memo(memo) => {
            let memo_str = match std::str::from_utf8(memo.as_slice()) {
                Ok(valid_str) => valid_str.to_string(),   // ← no sanitization
                Err(_) => hex::encode(memo.as_slice()),
            };
            match self {
                ConsentMessage::GenericDisplayMessage(message) => {
                    message.push_str(&format!("\n\n**Memo:**\n`{memo_str}`"));
                    //                                      ↑ single-backtick span, breakable
```

The memo is placed inside a single-backtick inline code span. In CommonMark, a code span opened with one backtick is closed by the next single backtick in the string. If `memo_str` contains a backtick, the span closes early and everything after it is rendered as normal markdown.

**How the memo reaches `add_memo`:**

The `build_icrc21_consent_info` function decodes the raw `TransferArg` bytes from the consent message request and passes the memo field directly: [2](#0-1) 

The `ConsentMessageBuilder::build()` then calls `message.add_memo(memo)` unconditionally if a memo is present: [3](#0-2) 

The public entry point is an `#[update]` method callable by any principal: [4](#0-3) 

**Concrete injection payload (22 bytes, within the 32-byte default limit):**

```
memo bytes = b"x`\n\n**To:**\n`attacker "
```

The format string produces:

```
\n\n**Memo:**\n`x`\n\n**To:**\n`attacker `
```

CommonMark parsing:
- `` `x` `` → inline code span (closes at the first backtick in the memo)
- `\n\n` → paragraph break
- `**To:**` → **bold label** rendered identically to the legitimate field
- `` `attacker ` `` → inline code span (closed by the trailing backtick from the format string)

The rendered output is visually indistinguishable from a legitimate `**To:**` field added by `add_account`.

**Memo size constraint:** The default ICRC-1 ledger limit is 32 bytes; the attack payload above is 22 bytes. Even on ledgers configured with a larger `max_memo_length`, the payload is trivially small. [5](#0-4) 

**Field ordering:** The legitimate `**To:**` field is added before the memo: [6](#0-5) 

So the rendered consent message would contain the real `**To:**` (attacker's address) followed by the injected fake `**To:**` (victim's expected address), or vice versa depending on the crafted payload. A user scanning quickly for the "To:" label would likely fixate on whichever one matches their expectation.

---

### Impact Explanation

ICRC-21 exists precisely to give wallets a ledger-generated, dApp-independent description of what is being signed. A malicious dApp that constructs a `TransferArg` with `to = attacker_address` and a crafted memo can cause the wallet's consent screen to display a fake `**To:**` field showing a victim-expected address, while the actual on-chain transfer goes to the attacker. The user approves what they believe is a legitimate transfer; funds are stolen.

The same technique can inject a fake `**Amount:**` field showing a smaller value than the real transfer, or inject a fake `**Fees:**` field, or inject arbitrary explanatory text designed to socially engineer approval.

---

### Likelihood Explanation

- The attacker is any unprivileged principal who can call `icrc21_canister_call_consent_message` (an `#[update]` endpoint with no access control).
- The attack requires only crafting a `TransferArg` with a memo containing a backtick — a trivial operation.
- The payload fits in the default 32-byte memo limit.
- No privileged role, key, or threshold corruption is required.
- The only prerequisite is that the victim's wallet renders `GenericDisplayMessage` as CommonMark markdown, which is the explicit intent of the ICRC-21 standard.

---

### Recommendation

Sanitize `memo_str` before interpolation. The minimal fix is to escape or strip backtick and newline characters before embedding the memo in the markdown string:

```rust
let safe_memo = memo_str
    .replace('`', "\\`")   // escape backticks
    .replace('\n', " ")    // collapse newlines
    .replace('\r', "");
message.push_str(&format!("\n\n**Memo:**\n`{safe_memo}`"));
```

Alternatively, hex-encode all memo bytes unconditionally for the `GenericDisplayMessage` branch (valid UTF-8 display is a convenience, not a requirement), or use a double-backtick span and strip any occurrence of ` `` ` from the memo. The `FieldsDisplayMessage` branch is not affected because it uses a structured `Value::Text` field that wallets render outside of markdown.

---

### Proof of Concept

```rust
#[test]
fn memo_markdown_injection() {
    use icrc_ledger_types::icrc21::lib::GenericMemo;
    use icrc_ledger_types::icrc21::responses::ConsentMessage;
    use serde_bytes::ByteBuf;

    let mut msg = ConsentMessage::GenericDisplayMessage(String::new());

    // Simulate add_account("To", "real_attacker_address")
    msg.add_account("To", "real_attacker_address".to_string());

    // Craft memo: closes the backtick span, injects a fake **To:** field
    let malicious_memo = b"x`\n\n**To:**\n`victim_expected_address ";
    msg.add_memo(GenericMemo::Icrc1Memo(ByteBuf::from(malicious_memo.to_vec())));

    if let ConsentMessage::GenericDisplayMessage(s) = &msg {
        // The string now contains a second **To:** section injected by the memo
        let to_count = s.matches("**To:**").count();
        assert_eq!(to_count, 2, "injected a fake To: field: {s}");
        // The fake address appears in the rendered output
        assert!(s.contains("victim_expected_address"));
    }
}
```

Running this test against the current code will pass, confirming the injection.

### Citations

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L284-295)
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
```

**File:** packages/icrc-ledger-types/src/icrc21/lib.rs (L205-207)
```rust
                message.add_amount(self.amount, self.decimals, &token_symbol)?;
                message.add_account("To", receiver_account.to_string());
                message.add_fee(
```

**File:** packages/icrc-ledger-types/src/icrc21/lib.rs (L290-292)
```rust
        if let Some(memo) = self.memo {
            message.add_memo(memo);
        }
```

**File:** packages/icrc-ledger-types/src/icrc21/lib.rs (L392-395)
```rust
            if let Some(memo) = memo {
                display_message_builder =
                    display_message_builder.with_memo(GenericMemo::Icrc1Memo(memo.0));
            }
```

**File:** rs/ledger_suite/icp/ledger/src/main.rs (L1478-1481)
```rust
#[update]
fn icrc21_canister_call_consent_message(
    consent_msg_request: ConsentMessageRequest,
) -> Result<ConsentInfo, Icrc21Error> {
```

**File:** rs/ledger_suite/tests/sm-tests/src/lib.rs (L1936-1941)
```rust
    // We didn't set the max_memo_length in the init params of the ledger
    // so the memo will be accepted only if it's 32 bytes or less.
    for i in 0..=32 {
        assert!(transfer_with_memo(&vec![0_u8; i]).is_ok(), "Memo size: {i}");
    }
    expect_memo_length_error(transfer_with_memo, &[0_u8; 33]);
```
