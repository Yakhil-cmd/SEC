The code evidence is clear and sufficient to make a determination. Let me trace the full path.

**Entrypoint:** `icrc21_canister_call_consent_message` in `rs/ledger_suite/icrc1/ledger/src/main.rs` is a public `#[update]` callable by any unprivileged principal. [1](#0-0) 

It collects `ledger_fee`, `token_symbol`, `token_name`, `decimals` — but **never reads the minting account from ledger state** — and passes them to `build_icrc21_consent_info_for_icrc1_and_icrc2_endpoints`. [2](#0-1) 

That wrapper calls `build_icrc21_consent_info` with `None` for the `transfer_args` / minting-account parameter.

In `build_icrc21_consent_info`, the `Icrc21Function::Transfer` branch decodes `TransferArg`, extracts `to`, and passes it directly to the builder with no check against the minting account: [3](#0-2) 

In `ConsentMessageBuilder::build()`, the `Transfer | GenericTransfer` arm unconditionally calls `add_intent(Icrc21Function::Transfer, ...)` (producing a "Send [token_name]" heading) and `add_account("To", receiver_account)` — there is no burn-detection branch anywhere: [4](#0-3) 

The minting account is never consulted at any point in the consent-message pipeline.

---

### Title
Consent message for `icrc1_transfer` to minting account displays "Send" instead of "Burn" — (`packages/icrc-ledger-types/src/icrc21/lib.rs`)

### Summary
`icrc21_canister_call_consent_message` never reads the ledger's minting account. When a caller supplies a `TransferArg` whose `to` field equals the minting account, the consent message unconditionally renders a "Send [token_name]" intent label. The actual `icrc1_transfer` execution treats any transfer to the minting account as a **burn** (irreversible supply reduction), so the consent message misrepresents the semantic operation.

### Finding Description
`icrc21_canister_call_consent_message` collects only `ledger_fee`, `token_symbol`, `token_name`, and `decimals` from ledger state. The minting account is never fetched and never passed into `build_icrc21_consent_info`. Inside that function, the `Icrc21Function::Transfer` branch decodes `TransferArg.to` and forwards it to `ConsentMessageBuilder::with_receiver_account` without any comparison to the minting account. `ConsentMessageBuilder::build()` then emits `add_intent(Icrc21Function::Transfer, …)` — always "Send" — regardless of whether `to` is the minting account. No burn-detection path exists anywhere in the consent-message library.

### Impact Explanation
A malicious dApp can present a wallet with a consent message reading "Send 100 ICP to `<minting_account>`". The user, trusting the ICRC-21 consent message as an accurate description of the operation, approves and signs. The actual `icrc1_transfer` call burns the tokens (reduces total supply, credits no one). The loss is irreversible. The ICRC-21 standard's core invariant — that the consent message faithfully describes the operation the user is authorizing — is violated.

### Likelihood Explanation
Any unprivileged principal can call `icrc21_canister_call_consent_message` with a crafted `TransferArg{to=minting_account}`. No special role, key, or governance majority is required. A malicious dApp or canister can trivially construct this scenario. The only prerequisite is knowing the minting account principal of the target ledger, which is publicly queryable via `icrc1_minting_account`.

### Recommendation
In `icrc21_canister_call_consent_message`, read the minting account from ledger state and pass it into `build_icrc21_consent_info`. Add a burn-detection branch in `build_icrc21_consent_info` (or in `ConsentMessageBuilder::build`): if `to == minting_account`, emit a "Burn [token_name]" intent label instead of "Send [token_name]", and omit the misleading "To" field (or label it "Burned from").

### Proof of Concept
```
// State-machine test sketch
let minting_account = ledger.icrc1_minting_account().unwrap();
let transfer_arg = TransferArg {
    to: minting_account,
    amount: Nat::from(1_000_000u64),
    fee: None, from_subaccount: None, memo: None, created_at_time: None,
};
let arg_bytes = Encode!(&transfer_arg).unwrap();
let request = ConsentMessageRequest {
    method: "icrc1_transfer".to_string(),
    arg: arg_bytes,
    user_preferences: /* GenericDisplay */,
};
let consent = ledger.icrc21_canister_call_consent_message(request).unwrap();
// FAILS: message contains "Send" not "Burn"
assert!(consent_text.contains("Burn"), "consent must label burn operations as burns");
```

### Citations

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

**File:** packages/icrc-ledger-types/src/icrc21/lib.rs (L182-213)
```rust
            Icrc21Function::Transfer | Icrc21Function::GenericTransfer => {
                let from_account = self.from.ok_or(Icrc21Error::GenericError {
                    error_code: Nat::from(500_u64),
                    description: "From account has to be specified.".to_owned(),
                })?;
                let receiver_account = self.receiver.ok_or(Icrc21Error::GenericError {
                    error_code: Nat::from(500_u64),
                    description: "Receiver account has to be specified.".to_owned(),
                })?;

                let token_symbol = self.token_symbol.ok_or(Icrc21Error::GenericError {
                    error_code: Nat::from(500_u64),
                    description: "Token Symbol must be specified.".to_owned(),
                })?;
                let token_name = self.token_name.ok_or(Icrc21Error::GenericError {
                    error_code: Nat::from(500_u64),
                    description: "Token Name must be specified.".to_owned(),
                })?;

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

**File:** packages/icrc-ledger-types/src/icrc21/lib.rs (L369-397)
```rust
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
        }
```
