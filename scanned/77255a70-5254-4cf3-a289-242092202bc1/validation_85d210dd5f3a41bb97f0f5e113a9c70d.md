### Title
Unsanitized Memo Interpolation Enables Markdown Field Injection in ICRC-21 Consent Messages — (`packages/icrc-ledger-types/src/icrc21/responses.rs`)

---

### Summary

`ConsentMessage::add_memo` interpolates raw, attacker-controlled UTF-8 memo bytes directly into a Markdown-formatted `GenericDisplayMessage` string without escaping backtick characters. Because the injected memo string can close the surrounding inline-code span and introduce new Markdown paragraphs, an attacker can synthesize display fields — including a fake `**To:**` field — that are visually indistinguishable from the real fields produced by `add_account`.

---

### Finding Description

`add_memo` in `responses.rs` formats the memo as:

```rust
message.push_str(&format!("\n\n**Memo:**\n`{memo_str}`"));
``` [1](#0-0) 

`add_account` uses the identical structural pattern:

```rust
message.push_str(&format!("\n\n**{name}:**\n`{account}`"))
``` [2](#0-1) 

If `memo_str` contains a backtick, it terminates the inline code span early. A memo value of:

```
foo`\n\n**To:**\n`attacker_address
```

produces the following raw string appended to the message:

```
\n\n**Memo:**\n`foo`\n\n**To:**\n`attacker_address`
```

When rendered as Markdown this is two separate paragraphs: `**Memo:** foo` and `**To:** attacker_address` — the second is byte-for-byte identical to what `add_account("To", ...)` emits.

The real `To` field is written at build time before the memo: [3](#0-2) 

The memo (and therefore the injected field) is appended last: [4](#0-3) 

On a hardware wallet that paginates `GenericDisplayMessage` screen-by-screen, the injected `**To:**` field appears on a later page, near the confirmation button, while the real `To` field is on an earlier page that the user has already scrolled past.

---

### Impact Explanation

An attacker who controls the `memo` field of a `TransferArg` can make the consent message display a fabricated recipient address. The attacker sets `TransferArg.to` to their own account and crafts a memo that injects a fake `**To:**` field showing a trusted or victim address. A user who approves the consent message on a hardware wallet (Ledger, etc.) that renders `GenericDisplayMessage` as Markdown will believe they are sending to the displayed address, while the actual on-chain transfer goes to the attacker's account.

---

### Likelihood Explanation

- The entrypoint is fully unprivileged: any caller can submit a `ConsentMessageRequest` with an arbitrary `arg` blob.
- The memo field in ICRC-1 `TransferArg` accepts arbitrary bytes up to 32 bytes; a backtick followed by newlines and Markdown fits within that limit.
- `GenericDisplayMessage` is explicitly designed for Markdown-capable wallets; hardware wallets that implement ICRC-21 are the primary consumers.
- No sanitization, escaping, or validation of memo content exists anywhere in the pipeline before interpolation.
- The injected field is structurally identical to a real field — there is no visual indicator that distinguishes it.

---

### Recommendation

Escape all Markdown special characters (at minimum backticks, asterisks, underscores, `#`, and newlines) in any user-controlled string before interpolating it into a `GenericDisplayMessage`. For memo content specifically, the safest approach is to always hex-encode the bytes unconditionally (removing the UTF-8 display path), or to apply a strict allowlist of printable ASCII characters that excludes all Markdown metacharacters before displaying as text.

---

### Proof of Concept

```rust
// memo bytes: foo`\n\n**To:**\n`evil_address
let memo_bytes = b"foo`\n\n**To:**\n`evil_address";
let memo = GenericMemo::Icrc1Memo(ByteBuf::from(memo_bytes.to_vec()));
let mut msg = ConsentMessage::GenericDisplayMessage(String::new());
msg.add_account("To", "real_attacker_address".to_string());
msg.add_memo(memo);

let rendered = match &msg {
    ConsentMessage::GenericDisplayMessage(s) => s.clone(),
    _ => panic!(),
};

// The rendered string contains a second "**To:**" paragraph
assert!(
    rendered.matches("**To:**").count() == 1,
    "FAIL: injected To field found — rendered:\n{}", rendered
);
// This assertion FAILS: count is 2
```

The test fails because the rendered `GenericDisplayMessage` contains two `**To:**` paragraphs — the real one and the injected one — and a Markdown-rendering wallet cannot distinguish them.

### Citations

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L89-91)
```rust
            ConsentMessage::GenericDisplayMessage(message) => {
                message.push_str(&format!("\n\n**{name}:**\n`{account}`"))
            }
```

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L293-295)
```rust
                    ConsentMessage::GenericDisplayMessage(message) => {
                        message.push_str(&format!("\n\n**Memo:**\n`{memo_str}`"));
                    }
```

**File:** packages/icrc-ledger-types/src/icrc21/lib.rs (L206-212)
```rust
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
