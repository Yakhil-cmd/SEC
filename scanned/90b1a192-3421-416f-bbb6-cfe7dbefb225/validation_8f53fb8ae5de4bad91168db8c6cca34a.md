### Title
ICRC-21 Consent Message Truncates u256 Token Amounts via `nat_to_u64`, Causing Incorrect Display or Hard Failure for Large Transfers - (`packages/icrc-ledger-types/src/icrc21/responses.rs`)

---

### Summary

The ICRC-21 consent message library converts token amounts to `u64` before embedding them in `FieldsDisplay` messages, and converts them to `f64` for `GenericDisplay` messages. Both conversions silently fail or lose precision when amounts exceed their respective type limits. Because the u256-token ICRC-1 ledger (used by ckETH and all ckERC20 tokens) supports amounts up to 2^256, any transfer exceeding `u64::MAX` (~18.4 ETH) causes the `FieldsDisplay` consent-message path to return a `GenericError` instead of a valid message, and the `GenericDisplay` path to display a silently rounded amount. This is the direct IC analog of the ZC-token decimal bug: both produce incorrect or unavailable amount representations in user-facing signing flows.

---

### Finding Description

In `packages/icrc-ledger-types/src/icrc21/responses.rs`, two private helpers are used to render token amounts in consent messages:

**`nat_to_u64`** (used by `FieldsDisplay` path):
```rust
fn nat_to_u64(tokens: Nat) -> Result<u64, Icrc21Error> {
    tokens.0.to_u64().ok_or(Icrc21Error::GenericError {
        error_code: Nat::from(500_u64),
        description: "Failed to convert tokens to u64".to_owned(),
    })
}
``` [1](#0-0) 

This is called from `add_amount`, `add_fee`, `add_allowance`, and `add_existing_allowance` whenever the caller requests `FieldsDisplay`: [2](#0-1) [3](#0-2) 

**`convert_tokens_to_string_representation`** (used by `GenericDisplay` path):
```rust
fn convert_tokens_to_string_representation(tokens: Nat, decimals: u8) -> Result<String, Icrc21Error> {
    let tokens = tokens.0.to_f64().ok_or(...)?;
    Ok(format!("{}", tokens / 10_f64.pow(decimals)))
}
``` [4](#0-3) 

The ICRC-1 ledger canister exposes `icrc21_canister_call_consent_message` as a public `#[update]` endpoint and passes the ledger's `icrc1_decimals()` and the transfer amount directly into these helpers: [5](#0-4) 

The ledger is compiled in two variants: [6](#0-5) 

The u256 variant is used for all ckERC20 tokens (ckETH, ckUSDC, etc.) and supports amounts up to 2^256. The `Value::TokenAmount` struct in the Candid interface hard-codes `amount: u64`: [7](#0-6) 

---

### Impact Explanation

**FieldsDisplay path (hard failure):** Any call to `icrc21_canister_call_consent_message` with `FieldsDisplay` and a transfer amount exceeding `u64::MAX` (18,446,744,073,709,551,615 = ~18.4 ETH at 18 decimals) returns `GenericError { error_code: 500, description: "Failed to convert tokens to u64" }`. Hardware wallets and ICRC-21 compliant signers that require `FieldsDisplay` (e.g., Ledger hardware wallet) cannot produce a consent message for the transaction and will refuse to sign. A user holding > 18.4 ckETH cannot use any ICRC-21 hardware wallet to authorize a full-balance transfer.

**GenericDisplay path (silent precision loss):** `to_f64()` on a `BigUint` never returns `None`; it silently rounds. For ckETH amounts above 2^53 wei (~0.009 ETH), the displayed amount loses precision. For example, a transfer of `9_007_199_254_740_993` wei (2^53 + 1 wei) displays as `"0.009007199254740992"` ETH — one wei short. While the rounding error is small in absolute terms, the consent message shown to the user is factually incorrect, undermining the trust guarantee that ICRC-21 is designed to provide.

---

### Likelihood Explanation

ckETH is a production mainnet canister. Transfers of > 18.4 ETH are routine for institutional users and DeFi protocols. Any such user attempting to use a hardware wallet via the `FieldsDisplay` path will encounter the hard failure. The `GenericDisplay` precision loss affects all ckETH transfers above ~0.009 ETH, which is essentially every non-trivial transfer. The entry point (`icrc21_canister_call_consent_message`) is a standard public `#[update]` endpoint reachable by any unprivileged ingress sender with no preconditions.

---

### Recommendation

1. Replace `nat_to_u64` with a `Nat`-native representation. The `Value::TokenAmount` variant should carry a `Nat` (or `u128` at minimum) rather than `u64` to accommodate u256 ledger amounts. Update the Candid `.did` files accordingly.
2. Replace the `f64` conversion in `convert_tokens_to_string_representation` with exact integer arithmetic: perform the decimal shift using `BigUint` division and modulo, then format the integer and fractional parts separately (as is already done correctly in `format_amount` in the ckBTC/ckDOGE minters).
3. Add integration tests for `icrc21_canister_call_consent_message` on the u256 ledger with amounts exceeding `u64::MAX`.

---

### Proof of Concept

**FieldsDisplay hard failure:**
```
// Craft a TransferArg with amount = u64::MAX + 1 = 18_446_744_073_709_551_616
// (a valid ckETH amount, ~18.4 ETH)
let large_amount = Nat::from(u64::MAX) + Nat::from(1_u64);
let transfer_arg = TransferArg {
    from_subaccount: None,
    to: some_account,
    amount: large_amount,
    fee: None,
    memo: None,
    created_at_time: None,
};
let request = ConsentMessageRequest {
    method: "icrc1_transfer".to_string(),
    arg: Encode!(&transfer_arg).unwrap(),
    user_preferences: ConsentMessageSpec {
        metadata: ConsentMessageMetadata { language: "en".to_string(), utc_offset_minutes: None },
        device_spec: Some(DisplayMessageType::FieldsDisplay),  // hardware wallet path
    },
};
// Call icrc21_canister_call_consent_message on the ckETH u256 ledger canister
// Result: Err(GenericError { error_code: 500, description: "Failed to convert tokens to u64" })
// Hardware wallet cannot sign — transaction is blocked.
```

The root cause is `nat_to_u64(amount)` at line 120 of `responses.rs` returning `None` for any `Nat > u64::MAX`, propagated as a `GenericError` that terminates the consent message flow. [8](#0-7) [1](#0-0)

### Citations

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L11-17)
```rust
#[derive(CandidType, Deserialize, Eq, PartialEq, Debug, Serialize, Clone)]
pub enum Value {
    TokenAmount {
        decimals: u8,
        amount: u64,
        symbol: String,
    },
```

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L116-126)
```rust
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

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L153-158)
```rust
            ConsentMessage::FieldsDisplayMessage(fields_display) => {
                let token_amount = Value::TokenAmount {
                    decimals,
                    amount: nat_to_u64(amount)?,
                    symbol: token_symbol.to_string(),
                };
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

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L81-85)
```rust
#[cfg(not(feature = "u256-tokens"))]
pub type Tokens = ic_icrc1_tokens_u64::U64;

#[cfg(feature = "u256-tokens")]
pub type Tokens = ic_icrc1_tokens_u256::U256;
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
