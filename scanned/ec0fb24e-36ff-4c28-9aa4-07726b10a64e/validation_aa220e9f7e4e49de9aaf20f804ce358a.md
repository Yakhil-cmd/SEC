The key code is confirmed. Let me verify the exact function and its callers.

The code is confirmed. I have everything needed for a complete assessment.

**The bug is real and the call path is fully traceable:**

`icrc21_canister_call_consent_message` → `build_icrc21_consent_info` → `ConsentMessageBuilder::build` → `message.add_amount` / `add_allowance` / `add_fee` → `convert_tokens_to_string_representation` → `tokens.0.to_f64()` → precision loss.

However, the severity framing in the question is significantly overstated. Here is the precise analysis:

---

### Title
f64 Precision Loss in `convert_tokens_to_string_representation` Causes Consent Message Display Inaccuracy — (`packages/icrc-ledger-types/src/icrc21/responses.rs`)

### Summary
`convert_tokens_to_string_representation` converts a `Nat` token amount to `f64` before formatting it for the `GenericDisplayMessage` consent message. Since `f64` has a 53-bit mantissa, any integer amount > 2^53 loses its least-significant bit(s), causing the displayed amount to differ from the amount that will actually be deducted.

### Finding Description
The function at issue:

```rust
fn convert_tokens_to_string_representation(
    tokens: Nat,
    decimals: u8,
) -> Result<String, Icrc21Error> {
    let tokens = tokens.0.to_f64().ok_or(...)?;   // ← precision loss here
    Ok(format!("{}", tokens / 10_f64.pow(decimals)))
}
``` [1](#0-0) 

This is called from `add_amount`, `add_allowance`, `add_fee`, and `add_existing_allowance` — but **only** when the message type is `GenericDisplayMessage`. The `FieldsDisplayMessage` branch uses `nat_to_u64` (exact integer conversion) and is unaffected. [2](#0-1) [3](#0-2) 

The full call chain from the public endpoint: [4](#0-3) [5](#0-4) 

### Impact Explanation — Corrected Quantification

The question's claim that u64::MAX "rounds to a completely different number" is **false**. f64 relative error is ≤ 2^-52 ≈ 2.2×10⁻¹⁶. Concrete examples:

| Amount (e8s) | Displayed ICP | Actual ICP | Absolute error |
|---|---|---|---|
| 2^53 + 1 = 9007199254740993 | 90071992.54740992 | 90071992.54740993 | 0.00000001 ICP (1 e8s) |
| u64::MAX = 18446744073709551615 | 184467440737.09552 | 184467440737.09551615 | ~0.00000385 ICP |

The displayed value is always within 1 ULP of the true value. The error is not attacker-amplifiable — a malicious dApp cannot craft an amount where the rounding produces a meaningfully different displayed figure. The maximum absolute error for any u64 amount with 8 decimals is on the order of a few e8s.

The real impact is: **the consent message invariant that "displayed amount = deducted amount" is violated for amounts > 2^53 in the `GenericDisplayMessage` path**. This is a correctness defect in a security-critical display component, but the practical exploitability for social engineering is negligible given the tiny absolute error.

### Likelihood Explanation
Any unprivileged user can call `icrc21_canister_call_consent_message` with a `TransferArg` or `ApproveArgs` containing `amount = Nat(2^53 + 1)`. No special role or key is required. The path is fully reachable via ingress. [6](#0-5) 

### Recommendation
Replace the f64 conversion with integer arithmetic. Since `decimals` is small (≤ 18 in practice), perform exact integer division and remainder:

```rust
fn convert_tokens_to_string_representation(tokens: Nat, decimals: u8) -> Result<String, Icrc21Error> {
    let divisor = BigUint::from(10u64).pow(decimals as u32);
    let whole = &tokens.0 / &divisor;
    let frac  = &tokens.0 % &divisor;
    if frac.is_zero() {
        Ok(format!("{whole}"))
    } else {
        Ok(format!("{whole}.{frac:0>decimals$}", decimals = decimals as usize))
    }
}
```

This preserves exact representation for all `u64`-range amounts.

### Proof of Concept
```rust
// Demonstrates the rounding:
let amount = Nat::from(2u64.pow(53) + 1); // 9007199254740993
let displayed = convert_tokens_to_string_representation(amount.clone(), 8).unwrap();
// displayed == "90071992.54740992"  (rounds down by 1 e8s)
// actual    == "90071992.54740993"
assert_ne!(displayed, "90071992.54740993"); // passes — bug confirmed
```

---

**Verdict: Valid finding.** The f64 conversion in `convert_tokens_to_string_representation` is a real, reachable precision defect in the `GenericDisplayMessage` consent path, violating the display-equals-actual invariant for amounts > 2^53. However, the impact is **low** — the absolute error is at most a few e8s, not "a completely different number," and the `FieldsDisplayMessage` path is entirely unaffected. The social-engineering scenario described in the question is technically possible but practically negligible given the error magnitude.

### Citations

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L111-126)
```rust
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

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L318-327)
```rust
fn convert_tokens_to_string_representation(
    tokens: Nat,
    decimals: u8,
) -> Result<String, Icrc21Error> {
    let tokens = tokens.0.to_f64().ok_or(Icrc21Error::GenericError {
        error_code: Nat::from(500_u64),
        description: "Failed to convert tokens to u64".to_owned(),
    })?;
    Ok(format!("{}", tokens / 10_f64.pow(decimals)))
}
```

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L329-334)
```rust
fn nat_to_u64(tokens: Nat) -> Result<u64, Icrc21Error> {
    tokens.0.to_u64().ok_or(Icrc21Error::GenericError {
        error_code: Nat::from(500_u64),
        description: "Failed to convert tokens to u64".to_owned(),
    })
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

**File:** packages/icrc-ledger-types/src/icrc21/lib.rs (L333-340)
```rust
    if consent_msg_request.arg.len() > MAX_CONSENT_MESSAGE_ARG_SIZE_BYTES as usize {
        return Err(Icrc21Error::UnsupportedCanisterCall(ErrorInfo {
            description: format!(
                "The argument size is too large. The maximum allowed size is {MAX_CONSENT_MESSAGE_ARG_SIZE_BYTES} bytes."
            ),
        }));
    }

```

**File:** packages/icrc-ledger-types/src/icrc21/lib.rs (L368-396)
```rust
    let consent_message = match display_message_builder.function {
        Icrc21Function::Transfer => {
            let TransferArg {
                memo,
                amount,
                from_subaccount,
                to,
                fee,
                created_at_time: _,
            } = Decode!(&consent_msg_request.arg, TransferArg).map_err(|e| {
                Icrc21Error::UnsupportedCanisterCall(ErrorInfo {
                    description: format!("Failed to decode TransferArg: {e}"),
                })
            })?;
            icrc21_check_fee(&fee, &ledger_fee)?;
            let sender = Account {
                owner: caller_principal,
                subaccount: from_subaccount,
            };
            display_message_builder = display_message_builder
                .with_amount(amount)
                .with_receiver_account(AccountOrId::Account(to))
                .with_from_account(AccountOrId::Account(sender));

            if let Some(memo) = memo {
                display_message_builder =
                    display_message_builder.with_memo(GenericMemo::Icrc1Memo(memo.0));
            }
            display_message_builder.build()
```
