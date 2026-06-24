The code is clear. Let me verify the exact injection mechanics before rendering a verdict.

### Title
Unsanitized Memo Bytes Enable Markdown Injection in ICRC-21 `GenericDisplayMessage` — (`packages/icrc-ledger-types/src/icrc21/responses.rs`)

---

### Summary

`add_memo` wraps the memo string in a single backtick pair but performs no escaping. A memo byte sequence that is valid UTF-8 and begins with a backtick character immediately closes the opening code span (producing an empty code span `\`\``), leaving the remainder of the memo content outside any code span and rendered as raw markdown by any wallet that displays `GenericDisplayMessage`.

---

### Finding Description

In `add_memo`, the `GenericDisplayMessage` branch formats the memo as:

```rust
message.push_str(&format!("\n\n**Memo:**\n`{memo_str}`"));
``` [1](#0-0) 

The only guard is a UTF-8 validity check; non-UTF-8 bytes are hex-encoded (safe), but valid UTF-8 is inserted verbatim:

```rust
let memo_str = match std::str::from_utf8(memo.as_slice()) {
    Ok(valid_str) => valid_str.to_string(),
    Err(_) => hex::encode(memo.as_slice()),
};
``` [2](#0-1) 

**Injection mechanics (CommonMark):** If `memo_str` starts with a backtick, the two consecutive backticks ` `` ` form an *empty* code span. Everything that follows — up to the trailing backtick appended by the format string — is outside any code span and is processed as markdown. For example, a memo whose UTF-8 content is:

```
`\n\n**Amount:** `0.00001 ICP`\n\n**To:**\n`legitimate_account`
```

produces the rendered string:

```
**Memo:**
``                          ← empty code span (harmless)

**Amount:** `0.00001 ICP`  ← bold header, injected by attacker

**To:**
`legitimate_account`        ← injected recipient
```

The memo is appended **after** the legitimate fields are already written by `build()`: [3](#0-2) 

The full `GenericDisplayMessage` therefore contains both the correct fields (amount, recipient) and the attacker-injected duplicate fields, all rendered as visually identical markdown.

The endpoint is a public `#[update]` callable by any unprivileged principal: [4](#0-3) 

The memo bytes flow from `TransferArg.memo` → `GenericMemo::Icrc1Memo` → `add_memo` without any sanitization step: [5](#0-4) 

---

### Impact Explanation

Any wallet that renders `GenericDisplayMessage` as markdown (which is the intended use of the field — the entire message is authored in markdown syntax) will display the injected fields as visually indistinguishable from the legitimate ones. A user presented with duplicate `**Amount:**` and `**To:**` sections may focus on the injected (attacker-controlled) values and approve a transaction whose real parameters differ.

**Important limitation:** the legitimate fields always appear *before* the memo section. A user who reads the full message top-to-bottom will see the correct values first. The deception relies on the user skimming, on the wallet truncating the message, or on the injected section being crafted to look like a "correction" of the earlier fields. This reduces the practical severity compared to a scenario where the injected content fully replaces the legitimate fields.

---

### Likelihood Explanation

- The endpoint is publicly callable with no authentication.
- Crafting a memo that starts with a backtick is trivial (one byte: `0x60`).
- The ICRC-21 standard explicitly intends `GenericDisplayMessage` to be rendered as markdown; wallets that comply are vulnerable to the visual confusion.
- The `FieldsDisplayMessage` variant is **not** affected — the memo is stored as a structured `Value::Text` field and never concatenated into a markdown string. [6](#0-5) 

---

### Recommendation

Before inserting `memo_str` into the `GenericDisplayMessage`, escape all markdown-significant characters (at minimum backticks, `*`, `_`, `#`, `[`, `]`). The simplest safe approach is to hex-encode the memo unconditionally in the `GenericDisplayMessage` branch, or to use a double-backtick fence and escape any internal double-backtick sequences.

---

### Proof of Concept

```rust
// memo bytes: backtick + injected markdown
let malicious_memo = b"`\n\n**Amount:** `0.00001 ICP`\n\n**To:**\n`legitimate_account`";
let transfer_arg = TransferArg {
    memo: Some(Memo(ByteBuf::from(malicious_memo.to_vec()))),
    amount: Nat::from(1_000_000_000_000_u64), // 10,000 ICP
    to: attacker_account,
    fee: None,
    from_subaccount: None,
    created_at_time: None,
};
// Call icrc21_canister_call_consent_message with method="icrc1_transfer"
// The resulting GenericDisplayMessage will contain both the real fields
// (10,000 ICP → attacker) and the injected fields (0.00001 ICP → legitimate_account).
```

The resulting `GenericDisplayMessage` string will contain attacker-controlled bold headers rendered identically to the legitimate ones, confirming the injection.

### Citations

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L288-291)
```rust
                let memo_str = match std::str::from_utf8(memo.as_slice()) {
                    Ok(valid_str) => valid_str.to_string(),
                    Err(_) => hex::encode(memo.as_slice()),
                };
```

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L293-295)
```rust
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
