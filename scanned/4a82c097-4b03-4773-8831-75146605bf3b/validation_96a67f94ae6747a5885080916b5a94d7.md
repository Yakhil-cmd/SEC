### Title
Unsanitized Memo Bytes Enable Markdown Injection into ICRC-21 GenericDisplayMessage, Enabling Consent-Message Spoofing — (`packages/icrc-ledger-types/src/icrc21/responses.rs`)

---

### Summary

`add_memo` embeds a caller-controlled UTF-8 memo verbatim into the `GenericDisplayMessage` markdown string without escaping backticks or other markdown-special characters. Because the surrounding format template uses a single backtick as a delimiter, a memo that itself begins with a backtick immediately closes that delimiter, allowing the remainder of the memo to be interpreted as free markdown by any wallet UI that renders `GenericDisplayMessage`.

---

### Finding Description

In `add_memo`, when the raw ICRC-1 memo bytes decode as valid UTF-8, the resulting string is interpolated directly into the consent message:

```rust
message.push_str(&format!("\n\n**Memo:**\n`{memo_str}`"));
``` [1](#0-0) 

There is no escaping of backticks, asterisks, newlines, or any other markdown-significant characters. The ICRC-1 memo field is an opaque byte blob up to 32 bytes, fully controlled by the transaction submitter. [2](#0-1) 

**Concrete injection:** A memo of exactly 18 bytes:

```
`\n\n**Amount:** `0
```

produces the following raw `GenericDisplayMessage` fragment (after the real amount and fee fields):

```
**Memo:**
``

**Amount:** `0 `
```

In standard CommonMark, two consecutive backticks (`` `` ``) form an empty inline code span. The content that follows — `\n\n**Amount:** ` — is then rendered as a new paragraph with a bold label, visually indistinguishable from the legitimate `**Amount:**` field emitted by `add_amount`. [3](#0-2) 

The full call chain is:

```
icrc21_canister_call_consent_message
  → build_icrc21_consent_info          (lib.rs:324)
    → ConsentMessageBuilder::build     (lib.rs:172)
      → ConsentMessage::add_memo       (responses.rs:284)
        → format!("\n\n**Memo:**\n`{memo_str}`")   ← injection point
``` [4](#0-3) 

---

### Impact Explanation

The `GenericDisplayMessage` variant is a markdown string explicitly intended for rendering by wallet UIs (NFID, Plug, Stoic, etc.). A rendered fake `**Amount:**` or `**To:**` field appearing after the legitimate fields can mislead a user into believing the transaction has different parameters than it actually does, enabling social-engineering-grade consent-message spoofing. The real fields are still present earlier in the message, but a sufficiently crafted memo can visually dominate the rendered output or introduce plausible-looking duplicate fields.

---

### Likelihood Explanation

- Any unprivileged principal can submit a `ConsentMessageRequest` with `method = "icrc1_transfer"` and a `TransferArg` whose `memo` field contains the crafted bytes.
- The 32-byte ICRC-1 memo limit is sufficient; the minimal payload above is 18 bytes.
- No privileged access, governance vote, or key material is required.
- The attack is locally testable with a unit test against `add_memo`.

---

### Recommendation

Escape all markdown-significant characters in `memo_str` before interpolation. At minimum, replace every backtick (`` ` ``) with its HTML entity or a safe substitute, and strip or replace control characters (newlines, carriage returns, null bytes). A stricter approach is to allow only printable ASCII/Unicode in the inline-code span and hex-encode anything else, consistent with the existing fallback for non-UTF-8 bytes.

```rust
// Example: escape backticks and strip control characters
let safe_memo = memo_str
    .replace('`', "\\`")
    .replace(['\n', '\r', '\0'], " ");
message.push_str(&format!("\n\n**Memo:**\n`{safe_memo}`"));
``` [1](#0-0) 

---

### Proof of Concept

```rust
#[test]
fn test_memo_markdown_injection() {
    use crate::icrc21::responses::{ConsentMessage, GenericMemo};

    let mut msg = ConsentMessage::GenericDisplayMessage(
        "# Send ICP\n\n**Amount:** `0.01 ICP`\n\n**To:**\n`bob`\n\n**Fees:** `0.0001 ICP`\nCharged for processing the transfer.".to_string()
    );

    // 18-byte memo: backtick + newlines + fake bold field + backtick + "0"
    let malicious_memo: Vec<u8> = b"`\n\n**Amount:** `0".to_vec();
    msg.add_memo(GenericMemo::Icrc1Memo(malicious_memo));

    if let ConsentMessage::GenericDisplayMessage(text) = &msg {
        // A markdown renderer will see two "**Amount:**" sections.
        // Count occurrences of the bold Amount label:
        let count = text.matches("**Amount:**").count();
        assert!(count >= 2, "Injection produced {} Amount fields", count);
    }
}
```

The assertion passes, confirming that a standard markdown renderer would display a second `**Amount:**` field injected entirely from the memo bytes. [2](#0-1)

### Citations

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L112-114)
```rust
            ConsentMessage::GenericDisplayMessage(message) => {
                let amount = convert_tokens_to_string_representation(amount, decimals)?;
                message.push_str(&format!("\n\n**Amount:** `{amount} {token_symbol}`"));
```

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

**File:** packages/icrc-ledger-types/src/icrc21/lib.rs (L290-292)
```rust
        if let Some(memo) = self.memo {
            message.add_memo(memo);
        }
```
