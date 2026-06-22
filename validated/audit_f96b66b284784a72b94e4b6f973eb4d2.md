### Title
Memo Markdown Injection in `add_memo` Allows Fake Field Labels in `GenericDisplayMessage` — (`packages/icrc-ledger-types/src/icrc21/responses.rs`)

### Summary

`add_memo` wraps the memo string in a single backtick pair but does not escape backtick characters within the memo. A memo containing a backtick closes the inline code span early, allowing arbitrary markdown — including fake `**To:**` field labels — to be injected into the `GenericDisplayMessage` consent message. The real recipient field is still rendered correctly, but a second attacker-controlled "To:" section appears after the fee line, which can confuse users into approving a transfer to the wrong address.

---

### Finding Description

In `add_memo`, when the display type is `GenericDisplayMessage`, the memo string is interpolated unsanitized:

```rust
message.push_str(&format!("\n\n**Memo:**\n`{memo_str}`"));
``` [1](#0-0) 

If `memo_str` contains a backtick (ASCII `0x60`, valid UTF-8, valid ICRC-1 memo byte), the inline code span closes early. For example, with memo bytes `x`\n\n**To:**\n`attacker` (21 bytes, within the 32-byte default limit), the appended string becomes:

```
\n\n**Memo:**\n`x`\n\n**To:**\n`attacker`
```

In CommonMark this renders as two separate fields: a `Memo:` field containing `x`, followed by a new `To:` field containing `attacker`. The same pattern applies to `add_account`, which uses the identical backtick-wrapping format:

```rust
message.push_str(&format!("\n\n**{name}:**\n`{account}`"))
``` [2](#0-1) 

The consent message build order for `icrc1_transfer` is: intent → From → Amount → **To** (real recipient) → Fee → **Memo** (injection point): [3](#0-2) 

The memo is appended last: [4](#0-3) 

The attacker-controlled injection therefore appears **after** the legitimate `To:` field. The full rendered message for a malicious dApp scenario looks like:

```
# Send Token
You are approving a transfer of funds from your account.

**From:**
`victim_account`

**Amount:** `100 TOKEN`

**To:**
`ATTACKER_ADDRESS`       ← real recipient (correct, but user may scroll past)

**Fee:** `0.0001 TOKEN`

**Memo:**
x

**To:**
`FAKE_VICTIM_ADDRESS`    ← injected by memo
```

The `FieldsDisplayMessage` path is **not** affected because the memo is stored as a structured `Value::Text` field, not interpolated into a markdown string. [5](#0-4) 

---

### Impact Explanation

The ICRC-21 consent message is the primary mechanism by which wallets protect users from malicious dApps. A malicious dApp that controls the transaction args (recipient + memo) can inject a fake `**To:**` section into the consent message. A user who does not read the full message top-to-bottom — or whose wallet scrolls to the bottom — may approve a transfer to the attacker's address while believing they are sending to a legitimate address. The `FieldsDisplayMessage` variant is immune; only wallets using `GenericDisplayMessage` with markdown rendering are affected.

---

### Likelihood Explanation

**Reduced by:**
- The real `To:` field is still rendered correctly and appears **before** the injected content. A careful user sees both.
- The default ICRC-1 memo limit is 32 bytes, leaving only ~18 bytes for the fake address after the injection prefix — insufficient for a full convincing principal text representation.
- Requires a malicious dApp as the proximate attacker; a random unprivileged user cannot inject into another user's consent message.

**Increased by:**
- The injection is technically trivial and locally testable with a single unit test.
- Wallets that display only the last few fields, or that auto-scroll to the bottom, would show the injected `To:` field prominently.
- The memo size limit is configurable (`max_memo_length` can be raised to 64 bytes or more), giving more room for a convincing fake address.
- The ICRC-21 consent message is specifically designed to be the trust anchor against malicious dApps; undermining it is the exact threat model it is supposed to prevent.

---

### Recommendation

Escape backtick characters in `memo_str` before interpolation. The simplest fix is to replace any `` ` `` in the memo string with a safe representation (e.g., `\`` or the Unicode escape `&#96;`) before inserting it into the format string. Alternatively, use a double-backtick code span (` `` `) and ensure the memo content cannot contain ` `` `. The `FieldsDisplayMessage` path requires no change.

---

### Proof of Concept

```rust
#[test]
fn test_memo_markdown_injection() {
    use crate::icrc21::lib::{ConsentMessageBuilder, GenericMemo};
    use crate::icrc21::responses::ConsentMessage;
    use icrc_ledger_types::icrc1::account::Account;
    use candid::Principal;

    // Craft memo: closes the backtick span and injects a fake "To:" field
    // Total: 21 bytes, within the 32-byte default limit
    let malicious_memo = b"x`\n\n**To:**\n`attacker".to_vec();

    let mut msg = ConsentMessage::GenericDisplayMessage(String::new());
    msg.add_memo(GenericMemo::Icrc1Memo(malicious_memo.into()));

    let rendered = match &msg {
        ConsentMessage::GenericDisplayMessage(s) => s.clone(),
        _ => panic!("wrong variant"),
    };

    // The injected "To:" section must NOT appear in the output
    assert!(
        !rendered.contains("**To:**"),
        "Markdown injection succeeded: {rendered}"
    );
}
```

This test **fails** against the current code, confirming the injection. [6](#0-5)

### Citations

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L89-91)
```rust
            ConsentMessage::GenericDisplayMessage(message) => {
                message.push_str(&format!("\n\n**{name}:**\n`{account}`"))
            }
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

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L296-299)
```rust
                    ConsentMessage::FieldsDisplayMessage(fields_display) => fields_display
                        .fields
                        .push(("Memo".to_string(), Value::Text { content: memo_str })),
                }
```

**File:** packages/icrc-ledger-types/src/icrc21/lib.rs (L201-212)
```rust
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

**File:** packages/icrc-ledger-types/src/icrc21/lib.rs (L290-292)
```rust
        if let Some(memo) = self.memo {
            message.add_memo(memo);
        }
```
