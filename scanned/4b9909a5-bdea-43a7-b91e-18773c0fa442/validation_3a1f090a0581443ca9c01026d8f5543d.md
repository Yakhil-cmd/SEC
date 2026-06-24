The key code is already visible. Let me read the full `add_memo` function and the surrounding context to confirm the exact exploit path.

### Title
Unsanitized Memo Bytes Enable Markdown Injection into ICRC-21 `GenericDisplayMessage` — (`packages/icrc-ledger-types/src/icrc21/responses.rs`)

---

### Summary

The `add_memo` function in `ConsentMessage` embeds attacker-controlled memo bytes directly into a markdown-formatted `GenericDisplayMessage` string without escaping backticks or other markdown-special characters. A malicious dApp can craft a memo whose valid UTF-8 content closes the opening backtick fence and injects visually indistinguishable fake transaction fields (bold `**Amount:**`, `**To:**`, etc.) into the consent message shown to the user.

---

### Finding Description

In `packages/icrc-ledger-types/src/icrc21/responses.rs`, `add_memo` converts the memo bytes to a UTF-8 string (falling back to hex only on invalid UTF-8) and then interpolates it verbatim:

```rust
// responses.rs line 288-294
let memo_str = match std::str::from_utf8(memo.as_slice()) {
    Ok(valid_str) => valid_str.to_string(),
    Err(_) => hex::encode(memo.as_slice()),
};
// ...
message.push_str(&format!("\n\n**Memo:**\n`{memo_str}`"));
``` [1](#0-0) 

The template wraps `memo_str` in a single backtick pair: `` `{memo_str}` ``. If `memo_str` begins with a backtick character, the two adjacent backticks (`` `` ``) form an **empty inline-code span** in CommonMark, and everything that follows is rendered as raw markdown — outside any code fence.

The `TransferArg` memo field is decoded directly from the caller-supplied `arg` blob with no sanitization:

```rust
// lib.rs lines 392-394
if let Some(memo) = memo {
    display_message_builder =
        display_message_builder.with_memo(GenericMemo::Icrc1Memo(memo.0));
}
``` [2](#0-1) 

---

### Impact Explanation

A malicious dApp constructs a `TransferArg` whose `memo` bytes are valid UTF-8 starting with a backtick followed by newlines and fake bold-field markdown. The ledger's `icrc21_canister_call_consent_message` returns a `GenericDisplayMessage` that contains the real transaction fields **and** the injected fake fields, rendered identically by any markdown-capable wallet UI. A user who does not read the entire message — or who is shown only the bottom portion of a long message — may approve a transaction believing it has different parameters (amount, recipient) than the actual on-chain transfer.

The `FieldsDisplayMessage` variant is **not** affected because the memo is stored as a structured `Value::Text` field, not interpolated into a markdown string. [3](#0-2) 

---

### Likelihood Explanation

- Entry point is fully unprivileged: any caller can invoke `icrc21_canister_call_consent_message` with an arbitrary `arg` blob.
- The memo field in ICRC-1 `TransferArg` accepts arbitrary bytes up to 32 bytes; a backtick + newline + fake fields fits within that limit.
- The attack requires a malicious dApp that controls the `TransferArg` it submits for consent — which is precisely the threat model ICRC-21 is designed to defend against.
- Wallets that render `GenericDisplayMessage` as markdown (the intended rendering per the ICRC-21 spec) are directly affected.

---

### Recommendation

Escape all backtick characters (and optionally other markdown-special characters) in `memo_str` before interpolation, for example:

```rust
let escaped = memo_str.replace('`', "\\`");
message.push_str(&format!("\n\n**Memo:**\n`{escaped}`"));
```

Alternatively, use a fenced code block (triple backticks) and escape any triple-backtick sequences inside the memo, which is more robust for multi-line content.

---

### Proof of Concept

Memo bytes (≤ 32 bytes, valid UTF-8):

```
`\n\n**Amount:** `999999 ICP`\n\n**To:** `attacker_account`\n\n**X:**
```

Resulting `GenericDisplayMessage` (abbreviated):

```
# Send Internet Computer
...
**Amount:** `1 ICP`

**To:**
`real_recipient`

**Fees:** `0.0001 ICP`
...

**Memo:**
``

**Amount:** `999999 ICP`

**To:** `attacker_account`

**X:**`
```

The injected `**Amount:**` and `**To:**` lines are visually identical to the ledger-generated fields above them. A wallet rendering this markdown would display two sets of transaction fields; a user seeing only the lower portion of the message would read the attacker-controlled values. [4](#0-3)

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

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L296-299)
```rust
                    ConsentMessage::FieldsDisplayMessage(fields_display) => fields_display
                        .fields
                        .push(("Memo".to_string(), Value::Text { content: memo_str })),
                }
```

**File:** packages/icrc-ledger-types/src/icrc21/lib.rs (L392-395)
```rust
            if let Some(memo) = memo {
                display_message_builder =
                    display_message_builder.with_memo(GenericMemo::Icrc1Memo(memo.0));
            }
```
