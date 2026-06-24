Audit Report

## Title
Markdown Injection in ICRC-21 `GenericDisplayMessage` via Unsanitized `memo` in `add_memo` — (`packages/icrc-ledger-types/src/icrc21/responses.rs`)

## Summary

`add_memo` in `packages/icrc-ledger-types/src/icrc21/responses.rs` decodes raw ICRC-1 memo bytes as UTF-8 and interpolates the result verbatim into a single-backtick inline code span in the `GenericDisplayMessage` string without escaping backtick characters. Any unprivileged ingress sender or malicious dapp can craft a memo whose first byte is a backtick (`` ` ``) to terminate the code span and inject arbitrary Markdown headings and fields into the ICRC-21 consent dialog, directly undermining the security guarantee that ICRC-21 is designed to provide. The `token_name`/`token_symbol` vectors (Vector 1) are not independently valid because a canister developer who controls the ledger canister can already return arbitrary consent messages without exploiting this specific code path; that vector is a trusted-operator concern, not a new attack surface.

## Finding Description

**Root cause — `add_memo` (Vector 2):**

`add_memo` at `packages/icrc-ledger-types/src/icrc21/responses.rs` lines 284–299 decodes the memo bytes as UTF-8 and places the result directly inside a single-backtick span:

```rust
let memo_str = match std::str::from_utf8(memo.as_slice()) {
    Ok(valid_str) => valid_str.to_string(),
    Err(_) => hex::encode(memo.as_slice()),
};
// ...
message.push_str(&format!("\n\n**Memo:**\n`{memo_str}`"));
```

A backtick anywhere in `memo_str` terminates the inline code span at that point. Everything after it is interpreted as raw Markdown by any wallet that renders `GenericDisplayMessage` as Markdown (which is the intended rendering mode — the entire message is Markdown-formatted with `#` headings, `**bold**` labels, and `` ` `` code spans).

**Exploit flow:**

1. Malicious dapp constructs an `icrc1_transfer` with `amount = 1 e8s`, `to = attacker_account`, and `memo = b"\`\n\n# You are sending 1000 ICP to attacker\n\n**To:**\n\`attacker_address"`.
2. Wallet calls `icrc21_canister_call_consent_message` with these args.
3. `build_icrc21_consent_info` at `packages/icrc-ledger-types/src/icrc21/lib.rs` lines 324–495 decodes the `TransferArg`, passes the memo to `add_memo`, which emits: `\n\n**Memo:**\n``\n\n# You are sending 1000 ICP to attacker\n\n**To:**\n`attacker_address`
4. The first backtick closes the `**Memo:**` code span. The injected heading and fake "To" field are rendered as top-level Markdown.
5. The real `**Amount:**` and `**To:**` fields appended afterward appear below the injected content and may be scrolled off-screen.
6. User approves based on the spoofed dialog; the actual transaction (1 e8s to attacker) executes.

**Why existing checks are insufficient:**

The only guard in `build_icrc21_consent_info` is a size check (`MAX_CONSENT_MESSAGE_ARG_SIZE_BYTES = 500`). There is no character-level validation or escaping of the memo content before it is interpolated into the Markdown string. The ckBTC and ckDOGE minters explicitly call out this exact risk in their `validate_address` comments at `rs/bitcoin/ckbtc/minter/src/updates/icrc21.rs` lines 195–209 and `rs/dogecoin/ckdoge/minter/src/updates/icrc21.rs` lines 196–210, but the shared ICRC-1/ICRC-2 library applies no equivalent guard.

## Impact Explanation

The attack allows a malicious dapp — an unprivileged actor — to inject fake transaction fields (recipient address, amount, heading) into the ICRC-21 consent dialog of any ICRC-1 ledger that uses the shared `build_icrc21_consent_info` library. A user who approves based on the spoofed dialog authorizes a transaction with different parameters than displayed, which can result in direct loss of ledger assets. This is a significant ICRC ledger security impact with concrete user harm, qualifying as **Medium ($200–$2,000)**. The constraints — a malicious dapp and a wallet that renders `GenericDisplayMessage` as Markdown — are realistic but not trivial, placing this below High.

## Likelihood Explanation

Submitting an ICRC-1 transfer with a crafted memo requires only an unprivileged ingress call; no special privileges are needed. Malicious dapps that construct transactions on behalf of users are a standard DeFi pattern. The ICRC-21 standard explicitly uses Markdown formatting throughout `GenericDisplayMessage` (headings, bold labels, code spans), so wallets implementing the standard are expected to render it as Markdown. The attack is repeatable and silent — the ledger canister behaves correctly from its own perspective.

## Recommendation

1. **Escape backticks in `memo_str`** before interpolating into the code span: replace every `` ` `` with `` \` ``. Alternatively, switch to a fenced code block (` ``` `…` ``` `), which is immune to single-backtick injection.
2. **Strip or escape Markdown-significant characters** (`\n`, `\r`, `#`, `*`, `` ` ``) from `token_name` and `token_symbol` before interpolating them into `add_intent` and the amount/fee/allowance methods, as a defense-in-depth measure consistent with the pattern already applied in the ckBTC and ckDOGE minters.
3. **Prefer `FieldsDisplayMessage`** for structured data: the `FieldsDisplay` variant carries typed `Value` entries that wallets render without Markdown interpretation, eliminating the injection surface entirely.

## Proof of Concept

```rust
// Craft a memo that breaks out of the backtick code span
let malicious_memo: Vec<u8> =
    b"`\n\n# You are sending 1000 ICP to attacker\n\n**To:**\n`attacker_address"
    .to_vec();

let transfer_args = TransferArg {
    memo: Some(Memo::from(ByteBuf::from(malicious_memo))),
    amount: Nat::from(1_u64),   // actual: 1 e8s to attacker
    to: attacker_account,
    fee: None,
    from_subaccount: None,
    created_at_time: None,
};

// Encode and call icrc21_canister_call_consent_message with
// method = "icrc1_transfer", arg = Encode!(&transfer_args).
//
// add_memo produces:
//   \n\n**Memo:**\n`  <- code span opened
//   `                 <- immediately closed by first byte of memo
//   \n\n# You are sending 1000 ICP to attacker
//   \n\n**To:**\n`attacker_address`
//
// Rendered Markdown shows a fake H1 heading and fake "To" field
// above the real Amount/To/Fees fields.
```

A deterministic unit test can be added to `packages/icrc-ledger-types/src/icrc21/` asserting that `add_memo` with a backtick-containing UTF-8 memo does **not** produce a `GenericDisplayMessage` containing an unescaped backtick outside the intended code span.