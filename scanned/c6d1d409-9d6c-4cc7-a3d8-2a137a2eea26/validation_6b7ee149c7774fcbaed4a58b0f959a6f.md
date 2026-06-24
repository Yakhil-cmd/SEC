### Title
Memo Backtick Injection Breaks Out of Inline Code Span, Injecting Arbitrary Markdown Fields into GenericDisplayMessage — (`packages/icrc-ledger-types/src/icrc21/responses.rs`)

---

### Summary

`add_memo` wraps the user-supplied memo string in a single-backtick inline code span (`` `{memo_str}` ``) to prevent markdown injection. However, if the memo bytes contain a backtick character, the code span is closed prematurely, and the remaining memo content is rendered as raw markdown. An unprivileged user can craft a 32-byte-or-fewer ICRC-1 memo that injects a visually authentic `**To:**` or `**From:**` field into the consent message, spoofing transaction details in any wallet that renders `GenericDisplayMessage` as markdown.

---

### Finding Description

In `add_memo`, the `GenericDisplayMessage` branch appends:

```rust
message.push_str(&format!("\n\n**Memo:**\n`{memo_str}`"));
``` [1](#0-0) 

The only guard applied to `memo_str` is a UTF-8 validity check; if the bytes are valid UTF-8 they are used verbatim, otherwise hex-encoded:

```rust
let memo_str = match std::str::from_utf8(memo.as_slice()) {
    Ok(valid_str) => valid_str.to_string(),
    Err(_) => hex::encode(memo.as_slice()),
};
``` [2](#0-1) 

No backtick escaping is performed. In CommonMark, a single-backtick code span is closed by the **next** single backtick that is not adjacent to another backtick. If `memo_str` contains a backtick, the opening backtick from the format template pairs with that interior backtick, closing the code span early. Everything between that interior backtick and the template's closing backtick is emitted as raw markdown.

**Concrete payload (17 bytes, valid UTF-8, within ICRC-1's 32-byte memo limit):**

```
x`\n\n**To:**\n`evil
```

The resulting `GenericDisplayMessage` string becomes:

```
\n\n**Memo:**\n`x`\n\n**To:**\n`evil`
```

CommonMark parse:
| Segment | Interpretation |
|---|---|
| `` `x` `` | inline code span — memo value `x` |
| `\n\n**To:**\n` | new paragraph, bold heading **To:** |
| `` `evil` `` | inline code span — fake recipient `evil` |

The rendered output is visually indistinguishable from a legitimate `**To:**` field produced by `add_account`:

```rust
message.push_str(&format!("\n\n**{name}:**\n`{account}`"))
``` [3](#0-2) 

The `GenericDisplayMessage` format is explicitly markdown (uses `# heading`, `**bold**`, `` `code` `` throughout `add_intent`, `add_account`, `add_amount`, `add_fee`), so wallets are expected to render it as such. [4](#0-3) 

The memo flows from `TransferArg.memo` → `build_icrc21_consent_info` → `ConsentMessageBuilder.with_memo` → `message.add_memo(memo)` with no sanitization at any step: [5](#0-4) 

---

### Impact Explanation

A wallet user is shown a consent message that appears to contain a legitimate `**To:**` (or `**From:**`, `**Amount:**`, etc.) field fabricated by the attacker. The user believes they are approving a transfer to a different recipient than the actual `to` field in the `TransferArg`. This is a direct UI-spoofing attack on the transaction signing flow, the primary security boundary ICRC-21 is designed to protect.

---

### Likelihood Explanation

- The attacker is fully unprivileged; only a standard `icrc1_transfer` or `icrc2_approve` call is needed to supply the memo.
- The payload is 17 bytes — well within the 32-byte ICRC-1 memo limit.
- All bytes are printable ASCII / valid UTF-8, passing the only guard in `add_memo`.
- Any wallet that renders `GenericDisplayMessage` as markdown (the intended use per the ICRC-21 spec and the explicit markdown formatting in the codebase) is affected.
- No privileged access, no key material, no social engineering of infrastructure is required.

---

### Recommendation

Escape all backtick characters in `memo_str` before interpolating into the markdown template. In CommonMark, a backtick inside a code span can be represented by using a longer backtick fence:

```rust
// Replace every ` with `` and wrap in `` ` `` ... `` ` ``
// Or simply replace backticks with their hex/unicode escape before display.
let safe_memo = memo_str.replace('`', "\\`");
message.push_str(&format!("\n\n**Memo:**\n`{safe_memo}`"));
```

Alternatively, use a double-backtick fence (` `` `) and ensure the memo content does not contain ` `` `. The most robust fix is to strip or percent-encode any character that has markdown significance (`\``, `*`, `_`, `#`, `[`, `]`) before inserting user-controlled content into a `GenericDisplayMessage`.

---

### Proof of Concept

```rust
// Memo bytes: x`\n\n**To:**\n`evil  (17 bytes, valid UTF-8)
let malicious_memo: Vec<u8> = b"x`\n\n**To:**\n`evil".to_vec();

// add_memo path:
// memo_str = "x`\n\n**To:**\n`evil"  (UTF-8 valid, passes guard)
// appended:  "\n\n**Memo:**\n`x`\n\n**To:**\n`evil`"

// Rendered markdown:
//   **Memo:**
//   `x`
//
//   **To:**
//   `evil`
//
// Visually identical to a legitimate To: field.
// The actual TransferArg.to field (the real recipient) appears earlier
// in the message; the injected field appears after Fees/Memo and
// a user scrolling quickly sees the last "To:" as authoritative.
```

The injected field appears **after** the real fields in the message, which is particularly dangerous: a user scrolling to the bottom of a long consent message sees the attacker-controlled `**To:**` last and may treat it as the definitive recipient.

### Citations

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L48-99)
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

    pub fn add_account(&mut self, name: &str, account: String) {
        match self {
            ConsentMessage::GenericDisplayMessage(message) => {
                message.push_str(&format!("\n\n**{name}:**\n`{account}`"))
            }
            ConsentMessage::FieldsDisplayMessage(fields_display) => fields_display.fields.push((
                name.to_string(),
                Value::Text {
                    content: account.to_string(),
                },
            )),
        }
    }
```

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L288-291)
```rust
                let memo_str = match std::str::from_utf8(memo.as_slice()) {
                    Ok(valid_str) => valid_str.to_string(),
                    Err(_) => hex::encode(memo.as_slice()),
                };
```

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L292-295)
```rust
                match self {
                    ConsentMessage::GenericDisplayMessage(message) => {
                        message.push_str(&format!("\n\n**Memo:**\n`{memo_str}`"));
                    }
```

**File:** packages/icrc-ledger-types/src/icrc21/lib.rs (L392-396)
```rust
            if let Some(memo) = memo {
                display_message_builder =
                    display_message_builder.with_memo(GenericMemo::Icrc1Memo(memo.0));
            }
            display_message_builder.build()
```
