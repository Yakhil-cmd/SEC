### Title
Unvalidated `utc_offset_minutes` Causes Canister Trap in `add_expiration` via `UtcOffset::from_whole_seconds(...).expect()` — (`packages/icrc-ledger-types/src/icrc21/responses.rs`)

---

### Summary

An unprivileged caller can trap the `icrc21_canister_call_consent_message` `#[update]` endpoint on any ICRC-1/ICRC-2 ledger by sending a `ConsentMessageRequest` with `utc_offset_minutes = Some(x)` where `|x| > 1439` and `expires_at = Some(T)` in the encoded `ApproveArgs`. The `i16` field accepts values up to ±32767, but `time::UtcOffset::from_whole_seconds` only accepts ±86399 seconds (±1439 minutes); the unconditional `.expect("Invalid offset")` panics on any out-of-range value, trapping the update call and burning ledger cycles.

---

### Finding Description

In `add_expiration`, the `GenericDisplayMessage` branch converts the caller-supplied `utc_offset_minutes: Option<i16>` directly into seconds and passes it to `UtcOffset::from_whole_seconds`, then calls `.expect()` with no prior bounds check: [1](#0-0) 

```rust
let offset = time::UtcOffset::from_whole_seconds(
    (utc_offset_minutes.unwrap_or(0) * 60).into(),
)
.expect("Invalid offset");
```

`utc_offset_minutes` is typed as `i16` (range −32768 to +32767), but `UtcOffset::from_whole_seconds` only accepts values in the range −86399 to +86399 (i.e., ±1439 minutes). Any value with `|utc_offset_minutes| ≥ 1440` produces an `Err`, and `.expect()` panics, trapping the canister.

The panic is only reachable when:
1. `expires_at` is `Some(T)` — the closure is only executed inside `.map(|ts| { ... })` [2](#0-1) 
2. The display type is `GenericDisplay` (or `None`, which defaults to `GenericDisplay`) — the `FieldsDisplayMessage` branch does **not** use `UtcOffset::from_whole_seconds` [3](#0-2) 
3. The method is `icrc2_approve` — the only method that calls `add_expiration` [4](#0-3) 

No validation of `utc_offset_minutes` exists anywhere in the call chain. `build_icrc21_consent_info` passes the raw `i16` value through without bounds checking: [5](#0-4) 

The endpoint is `#[update]` on both the ICRC-1 ledger and the ICP ledger: [6](#0-5) [7](#0-6) 

---

### Impact Explanation

- Every `icrc2_approve` consent message request with `|utc_offset_minutes| ≥ 1440` **and** `expires_at = Some(T)` **and** `GenericDisplay` (the default) traps unconditionally.
- The trap rolls back state but burns cycles from the ledger canister on every call. An attacker can repeatedly send such requests to drain ledger cycles.
- Any wallet or dApp that passes a non-standard (but `i16`-valid) timezone offset alongside an expiring approval will receive a trap instead of a consent message, breaking ICRC-21 UX for all such approve flows.
- The `FieldsDisplayMessage` path is unaffected; only `GenericDisplay` (the default) is vulnerable.

---

### Likelihood Explanation

The attack requires no privilege, no key, and no governance majority. A single ingress message with a crafted `utc_offset_minutes` value (e.g., `1441`) and any valid `expires_at` timestamp is sufficient to trigger the trap. The `i16` type is part of the public Candid interface, so any caller can supply out-of-range values. The endpoint is publicly accessible on mainnet ledger canisters.

---

### Recommendation

Replace the unconditional `.expect()` with a graceful fallback. For example:

```rust
let offset = time::UtcOffset::from_whole_seconds(
    (utc_offset_minutes.unwrap_or(0) as i32) * 60,
)
.unwrap_or(time::UtcOffset::UTC);
```

Alternatively, validate `utc_offset_minutes` at the entry point in `build_icrc21_consent_info` and return an `Icrc21Error` if `|offset| > 1439`.

---

### Proof of Concept

**Unit test (triggers the panic directly):**
```rust
#[test]
#[should_panic(expected = "Invalid offset")]
fn test_add_expiration_panics_on_large_utc_offset() {
    let mut msg = ConsentMessage::GenericDisplayMessage(String::new());
    // expires_at = Some(1_620_332_230_000_000_000 ns), utc_offset_minutes = 1441
    msg.add_expiration(Some(1_620_332_230_000_000_000u64), Some(1441));
}
```

**State-machine test (end-to-end trap):**
```rust
let approve_args = ApproveArgs {
    spender: spender_account,
    amount: Nat::from(1_000_000_u32),
    expires_at: Some(1_620_332_230_000_000_000u64), // must be Some
    fee: None, expected_allowance: None, from_subaccount: None,
    memo: None, created_at_time: None,
};
let req = ConsentMessageRequest {
    method: "icrc2_approve".to_owned(),
    arg: Encode!(&approve_args).unwrap(),
    user_preferences: ConsentMessageSpec {
        metadata: ConsentMessageMetadata {
            language: "en".to_string(),
            utc_offset_minutes: Some(1441), // |1441| > 1439 → panic
        },
        device_spec: None, // defaults to GenericDisplay → vulnerable branch
    },
};
// The call must trap (reject), not return Ok or Err(Icrc21Error)
let result = env.execute_ingress_as(caller, ledger_id,
    "icrc21_canister_call_consent_message", Encode!(&req).unwrap());
assert!(result.is_err(), "expected trap, got success");
```

### Citations

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L230-231)
```rust
                let expires_at = expires_at
                    .map(|ts| {
```

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L247-250)
```rust
                        let offset = time::UtcOffset::from_whole_seconds(
                            (utc_offset_minutes.unwrap_or(0) * 60).into(),
                        )
                        .expect("Invalid offset");
```

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L262-280)
```rust
            ConsentMessage::FieldsDisplayMessage(fields_display) => {
                match expires_at {
                    Some(expires_at) => {
                        let seconds = (expires_at as i64) / 10_i64.pow(9);
                        fields_display.fields.push((
                            "Approval expiration".to_string(),
                            Value::TimestampSeconds {
                                amount: seconds as u64,
                            },
                        ))
                    }
                    None => fields_display.fields.push((
                        "Approval expiration".to_string(),
                        Value::Text {
                            content: "This approval does not have an expiration.".to_string(),
                        },
                    )),
                };
            }
```

**File:** packages/icrc-ledger-types/src/icrc21/lib.rs (L241-241)
```rust
                message.add_expiration(self.expires_at, self.utc_offset_minutes);
```

**File:** packages/icrc-ledger-types/src/icrc21/lib.rs (L356-362)
```rust
    if let Some(offset) = consent_msg_request
        .user_preferences
        .metadata
        .utc_offset_minutes
    {
        display_message_builder = display_message_builder.with_utc_offset_minutes(offset);
    }
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L1189-1206)
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
```

**File:** rs/ledger_suite/icp/ledger/src/main.rs (L1478-1541)
```rust
#[update]
fn icrc21_canister_call_consent_message(
    consent_msg_request: ConsentMessageRequest,
) -> Result<ConsentInfo, Icrc21Error> {
    let caller_principal = caller();
    let ledger_fee = Nat::from(LEDGER.read().unwrap().transfer_fee.get_e8s());
    let token_symbol = LEDGER.read().unwrap().token_symbol.clone();
    let token_name = LEDGER.read().unwrap().token_name.clone();
    let decimals = ic_ledger_core::tokens::DECIMAL_PLACES as u8;

    if consent_msg_request.method == "transfer" {
        let TransferArgs {
            memo,
            amount,
            fee,
            from_subaccount,
            to,
            created_at_time: _,
        } = Decode!(&consent_msg_request.arg, TransferArgs).map_err(|e| {
            Icrc21Error::UnsupportedCanisterCall(ErrorInfo {
                description: format!("Failed to decode TransferArgs: {e}"),
            })
        })?;
        icrc21_check_fee(&Some(Nat::from(fee)), &ledger_fee)?;
        let from = if caller() == Principal::anonymous() {
            AccountOrId::AccountIdAddress(None)
        } else {
            let account = Account {
                owner: caller(),
                subaccount: from_subaccount.map(|sa| sa.0),
            };
            AccountOrId::AccountIdAddress(Some(AccountIdentifier::from(account).to_hex()))
        };
        let receiver = AccountIdentifier::from_slice(&to).map_err(|e| {
            Icrc21Error::UnsupportedCanisterCall(ErrorInfo {
                description: format!("Failed to parse receiver account id: {e}"),
            })
        })?;
        let args = GenericTransferArgs {
            from,
            receiver: AccountOrId::AccountIdAddress(Some(receiver.to_hex())),
            amount: Nat::from(amount.get_e8s()),
            memo: Some(GenericMemo::IntMemo(memo.0)),
        };
        build_icrc21_consent_info(
            consent_msg_request,
            caller_principal,
            ledger_fee,
            token_symbol,
            token_name,
            decimals,
            Some(args),
        )
    } else {
        build_icrc21_consent_info_for_icrc1_and_icrc2_endpoints(
            consent_msg_request,
            caller_principal,
            ledger_fee,
            token_symbol,
            token_name,
            decimals,
        )
    }
}
```
