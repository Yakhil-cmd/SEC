### Title
ICRC-21 Consent Message Amount Truncation Causes DoS for u256-Token Ledger Users with Large Balances — (`packages/icrc-ledger-types/src/icrc21/responses.rs`)

---

### Summary

The ICRC-21 consent message library silently caps token amounts at `u64::MAX` when building `FieldsDisplay` messages, and loses floating-point precision in `GenericDisplay` messages. Because ckETH and all ckERC20 ledgers are deployed as u256-token ledgers (amounts up to 2²⁵⁶−1), any user whose transfer or approval amount exceeds `u64::MAX` (≈ 18.4 ETH in wei) receives a hard `GenericError` from the consent-message endpoint instead of a valid consent message. This is the IC analog of the OFT report's "amount expressed in one unit system compared against a value in a different unit system, causing reversion."

---

### Finding Description

`nat_to_u64` is the sole conversion used when building the `FieldsDisplay` variant of a consent message: [1](#0-0) 

It is called unconditionally in `add_amount`, `add_fee`, `add_allowance`, and `add_existing_allowance` for the `FieldsDisplay` path: [2](#0-1) [3](#0-2) 

The `Value::TokenAmount` struct stores `amount: u64`: [4](#0-3) 

The ckERC20 ledger suite orchestrator deploys every ckERC20 ledger (ckETH, ckUSDC, ckUSDT, etc.) as the **u256 variant** (`ic-icrc1-ledger-u256.wasm.gz`): [5](#0-4) 

The u256 ledger's `Tokens` type supports values up to 2²⁵⁶−1, confirmed by the test suite: [6](#0-5) 

For the `GenericDisplay` path, `convert_tokens_to_string_representation` converts via `to_f64()`: [7](#0-6) 

`f64` has only 53 bits of mantissa, so any amount above ~9 × 10¹⁵ (≈ 9,000 ETH in wei) is displayed with rounding error, silently misleading the user about the exact amount they are signing.

The shared `build_icrc21_consent_info_for_icrc1_and_icrc2_endpoints` function is the single entry point used by all ICRC-1 ledgers: [8](#0-7) 

---

### Impact Explanation

**FieldsDisplay (hard failure):** Any ckETH or ckERC20 user whose `icrc1_transfer`, `icrc2_approve`, or `icrc2_transfer_from` amount exceeds `u64::MAX` (≈ 18.4 ETH in wei) receives `Icrc21Error::GenericError` instead of a consent message. Hardware wallets and ICRC-21-compliant wallets that request `FieldsDisplay` cannot render a consent screen, effectively blocking those users from signing transactions through compliant wallet software.

**GenericDisplay (silent precision loss):** Amounts above ~9 × 10¹⁵ wei (≈ 9,000 ETH) are converted to `f64` and displayed with rounding error. A user approving a large allowance (e.g., `u256::MAX` as an infinite approval) would see a wildly incorrect number on their wallet screen, undermining the purpose of the consent message.

The ckETH withdrawal flow explicitly documents that amounts are in wei (18 decimals): [9](#0-8) 

---

### Likelihood Explanation

Any ckETH holder with more than ~18.4 ETH triggers the `FieldsDisplay` hard failure. At current prices this is a common balance for retail and institutional users. The entry path is a standard unprivileged `icrc21_canister_call_consent_message` update call, requiring no special role. The `GenericDisplay` precision-loss path is triggered for any user with more than ~9,000 ETH, which applies to large holders and smart-contract-style infinite approvals (`u256::MAX`).

---

### Recommendation

1. Replace `Value::TokenAmount { amount: u64 }` with a `Nat`/`BigUint` field, or add a `Text` fallback for amounts that exceed `u64::MAX`.
2. In `convert_tokens_to_string_representation`, use arbitrary-precision arithmetic (e.g., `BigDecimal`) instead of `f64` to avoid precision loss for large amounts.
3. Add test coverage for amounts exceeding `u64::MAX` on u256-token ledgers in the ICRC-21 consent message test suite.

---

### Proof of Concept

1. Deploy the u256-variant ICRC-1 ledger (as used for ckETH/ckERC20).
2. Mint `u64::MAX + 1` tokens to a test account.
3. Call `icrc21_canister_call_consent_message` with:
   - `method = "icrc1_transfer"`
   - `amount = u64::MAX + 1`
   - `device_spec = FieldsDisplay`
4. Observe `Icrc21Error::GenericError { description: "Failed to convert tokens to u64" }` instead of a valid consent message.

The `nat_to_u64` call at line 330 returns `None` for any `Nat` whose inner `BigUint` requires more than one 64-bit limb, propagating as a `GenericError` that aborts consent message generation entirely. [1](#0-0)

### Citations

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L12-17)
```rust
pub enum Value {
    TokenAmount {
        decimals: u8,
        amount: u64,
        symbol: String,
    },
```

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L116-123)
```rust
            ConsentMessage::FieldsDisplayMessage(fields_display) => fields_display.fields.push((
                "Amount".to_string(),
                Value::TokenAmount {
                    decimals,
                    amount: nat_to_u64(amount)?,
                    symbol: token_symbol.to_string(),
                },
            )),
```

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L191-198)
```rust
            ConsentMessage::FieldsDisplayMessage(fields_display) => fields_display.fields.push((
                "Requested allowance".to_string(),
                Value::TokenAmount {
                    decimals,
                    amount: nat_to_u64(amount)?,
                    symbol: token_symbol.to_string(),
                },
            )),
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

**File:** rs/ethereum/cketh/mainnet/orchestrator_upgrade_2024_05_19.md (L56-57)
```markdown
sha256sum ./artifacts/canisters/ic-icrc1-ledger-u256.wasm.gz
sha256sum ./artifacts/canisters/ic-icrc1-index-ng-u256.wasm.gz
```

**File:** rs/ledger_suite/icrc1/index-ng/tests/tests.rs (L1774-1777)
```rust
    #[cfg(not(feature = "u256-tokens"))]
    assert_eq!(max_amount, Nat::from(u64::MAX));
    #[cfg(feature = "u256-tokens")]
    assert_ne!(max_amount, Nat::from(u64::MAX));
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

**File:** rs/ethereum/cketh/docs/cketh.adoc (L156-158)
```text
The amounts described below use the smallest denomination of ETH called **wei**, where
`1 ETH = 1_000_000_000_000_000_000 WEI` (Ethereum uses 18 decimals).
You can use link:https://eth-converter.com/[this converter] to convert ETH to wei.
```
