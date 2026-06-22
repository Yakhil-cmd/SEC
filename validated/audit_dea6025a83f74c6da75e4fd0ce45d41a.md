### Title
Precision Loss and Denial-of-Service in ICRC-21 Consent Message Amount Display for Large Token Amounts - (File: `packages/icrc-ledger-types/src/icrc21/responses.rs`)

### Summary
The ICRC-21 consent message library uses `f64` arithmetic and `u64` truncation to display token amounts. For tokens with large amounts (e.g., ckETH with 18 decimals using the u256 ledger), this causes two distinct bugs: (1) silent precision loss in `GenericDisplayMessage` mode, and (2) a hard error/DoS in `FieldsDisplayMessage` mode when the amount exceeds `u64::MAX`. Any unprivileged user can trigger these by calling `icrc21_canister_call_consent_message` with a large transfer amount.

### Finding Description

In `packages/icrc-ledger-types/src/icrc21/responses.rs`, two private functions handle token amount display:

**Bug 1 — f64 precision loss in `convert_tokens_to_string_representation`:**

```rust
fn convert_tokens_to_string_representation(
    tokens: Nat,
    decimals: u8,
) -> Result<String, Icrc21Error> {
    let tokens = tokens.0.to_f64().ok_or(Icrc21Error::GenericError { ... })?;
    Ok(format!("{}", tokens / 10_f64.pow(decimals)))
}
```

`Nat` is an arbitrary-precision integer (`BigUint`). Converting it to `f64` silently loses precision for values beyond 2^53 (~9 × 10^15). For ckETH (18 decimals, u256 ledger), a transfer of `1_000_000_000_000_000_001` wei (1 ETH + 1 wei) is displayed as `1` ETH. The displayed amount in the consent message is wrong. [1](#0-0) 

**Bug 2 — `nat_to_u64` hard error for amounts > u64::MAX in `FieldsDisplayMessage` mode:**

```rust
fn nat_to_u64(tokens: Nat) -> Result<u64, Icrc21Error> {
    tokens.0.to_u64().ok_or(Icrc21Error::GenericError {
        error_code: Nat::from(500_u64),
        description: "Failed to convert tokens to u64".to_owned(),
    })
}
```

When `FieldsDisplayMessage` is requested, `add_amount`, `add_fee`, `add_allowance`, and `add_existing_allowance` all call `nat_to_u64(amount)?`. For u256 ledger tokens (ckETH, ckUSDC, etc.), any amount exceeding `u64::MAX` (≈ 18.4 ETH in wei) causes the entire `icrc21_canister_call_consent_message` call to return a `GenericError`, denying the user a consent message. [2](#0-1) 

The `Value::TokenAmount` struct itself stores `amount: u64`, confirming the design assumption that amounts fit in u64 — an assumption violated by u256 ledger tokens. [3](#0-2) 

The ICRC-1 ledger's `icrc21_canister_call_consent_message` endpoint calls `build_icrc21_consent_info_for_icrc1_and_icrc2_endpoints`, which routes through `ConsentMessageBuilder::build()` → `add_amount(self.amount, self.decimals, ...)`, directly hitting both bugs. [4](#0-3) 

The ckETH ledger is deployed as a u256 ledger (wasm `ic-icrc1-ledger-u256.wasm.gz`) with 18 decimals, making transfers > ~18.4 ETH routinely trigger Bug 2. [5](#0-4) 

### Impact Explanation

- **Bug 1 (GenericDisplay)**: The consent message shown to a user (e.g., in a hardware wallet or signing UI) displays a silently rounded/incorrect token amount. A user approving a transfer of `1_000_000_000_000_000_001` wei sees `1 ckETH` instead of `1.000000000000000001 ckETH`. This undermines the purpose of ICRC-21 (informed consent).
- **Bug 2 (FieldsDisplay)**: Any user requesting a `FieldsDisplayMessage` consent for a ckETH transfer exceeding ~18.4 ETH receives a `GenericError` instead of a consent message. This is a denial-of-service on the consent endpoint for a large class of legitimate transactions, breaking wallet integrations that rely on `FieldsDisplay`.

### Likelihood Explanation

ckETH is a live mainnet token with 18 decimals on a u256 ledger. Transfers of more than 18.4 ETH are routine for institutional users and DeFi protocols. Any wallet or dApp using `FieldsDisplay` mode (as recommended for hardware wallets per ICRC-21) will fail for such amounts. The entry point (`icrc21_canister_call_consent_message`) is a public update endpoint callable by any principal with no authentication requirement. [6](#0-5) 

### Recommendation

1. Replace `to_f64()` in `convert_tokens_to_string_representation` with exact arbitrary-precision decimal formatting using `BigUint` arithmetic (integer division and modulo by `10^decimals`) to avoid precision loss.
2. Replace `Value::TokenAmount { amount: u64 }` with a `Nat`/`BigUint`-backed representation, or use a string representation for the amount field, so that u256 token amounts are not truncated.
3. Until fixed, `nat_to_u64` should at minimum return a graceful fallback (e.g., string representation) rather than a hard error, so that consent messages remain available for large amounts.

### Proof of Concept

**Bug 2 (DoS)**: Call `icrc21_canister_call_consent_message` on the ckETH ledger (`ss2fx-dyaaa-aaaar-qacoq-cai`) with:
```
method = "icrc1_transfer"
arg = TransferArg {
    amount = 20_000_000_000_000_000_000,  // 20 ETH in wei, > u64::MAX? No...
```

Wait — `u64::MAX` = 18,446,744,073,709,551,615. In wei that is ~18.4 ETH. So:

```
amount = 18_446_744_073_709_551_616  // u64::MAX + 1 wei
user_preferences.device_spec = FieldsDisplay
```

The call returns `Err(GenericError { error_code: 500, description: "Failed to convert tokens to u64" })` instead of a consent message, because `nat_to_u64` fails at: [7](#0-6) 

**Bug 1 (precision loss)**: Call with `amount = 1_000_000_000_000_000_001` (1 ETH + 1 wei) and `GenericDisplay`. The returned message contains `**Amount:** \`1 ckETH\`` instead of the correct `1.000000000000000001 ckETH`, because `(1_000_000_000_000_000_001_u128 as f64) / 1e18 == 1.0` in IEEE 754 double precision. [8](#0-7)

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

**File:** rs/ethereum/cketh/mainnet/ledger_proposal.md (L1-24)
```markdown
# Proposal to Install the ckETH Ledger Canister

Git hash: `5ecbd59c6c9f9f874d4340f9fbbd96af07aa2576`

New compressed Wasm hash: `3148f7a9f1b0ee39262c8abe3b08813480cf78551eee5a60ab1cf38433b5d9b0`

Target canister: `ss2fx-dyaaa-aaaar-qacoq-cai`

---

## Motivation

This proposal install the mainnet ckETH ledger to the governance-controlled canister ID [`ss2fx-dyaaa-aaaar-qacoq-cai`](https://dashboard.internetcomputer.org/canister/ss2fx-dyaaa-aaaar-qacoq-cai) on subnet [`pzp6e-ekpqk-3c5x7-2h6so-njoeq-mt45d-h3h6c-q3mxf-vpeq5-fk5o7-yae`](https://dashboard.internetcomputer.org/subnet/pzp6e-ekpqk-3c5x7-2h6so-njoeq-mt45d-h3h6c-q3mxf-vpeq5-fk5o7-yae).

This proposal is equal to [126170](https://dashboard.internetcomputer.org/proposal/126170), except that it additionally sets the decimals to 18 and the memo size to 80, which were incorrectly omitted.

## Install args

```
git fetch
git checkout 5ecbd59c6c9f9f874d4340f9fbbd96af07aa2576
cd rs/rosetta-api/icrc1/ledger
didc encode -d ledger.did -t '(LedgerArg)' '(variant { Init = record { minting_account = record { owner = principal "sv3dd-oaaaa-aaaar-qacoa-cai" }; fee_collector_account = opt record { owner = principal "sv3dd-oaaaa-aaaar-qacoa-cai"; subaccount = opt blob "\00\00\00\00\00\00\00\00\00\00\00\00\00\00\00\00\00\00\00\00\00\00\00\00\00\00\00\00\00\00\0f\ee"; }; decimals = opt 18; max_memo_length = opt 80; transfer_fee = 2_000_000_000_000; token_symbol = "ckETH"; token_name = "ckETH"; feature_flags = opt record { icrc2 = true }; metadata = vec { record { "icrc1:logo"; variant { Text = "data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iMTQ2IiBoZWlnaHQ9IjE0NiIgdmlld0JveD0iMCAwIDE0NiAxNDYiIGZpbGw9Im5vbmUiIHhtbG5zPSJodHRwOi8vd3d3LnczLm9yZy8yMDAwL3N2ZyI+CjxyZWN0IHdpZHRoPSIxNDYiIGhlaWdodD0iMTQ2IiByeD0iNzMiIG ... (truncated)
```
```

**File:** rs/ledger_suite/icrc1/ledger/ledger.did (L631-631)
```text
  icrc21_canister_call_consent_message : (icrc21_consent_message_request) -> (icrc21_consent_message_response);
```
