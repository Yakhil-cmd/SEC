### Title
`nat_to_u64` Overflow Causes ICRC-21 Consent Message Failure for Large Token Amounts - (`packages/icrc-ledger-types/src/icrc21/responses.rs`)

### Summary

The `nat_to_u64` helper in the ICRC-21 consent message library silently fails when a token transfer amount exceeds `u64::MAX`. For tokens with 18 decimals (e.g., ckETH), this threshold is only ~18.44 tokens. Any user requesting a `FieldsDisplay` consent message for a transfer above this threshold receives a `GenericError`, making the `icrc21_canister_call_consent_message` endpoint permanently unusable for large-value transfers on high-decimal tokens.

### Finding Description

The `FieldsDisplayMessage` branch of `add_amount`, `add_fee`, `add_allowance`, and `add_existing_allowance` all call `nat_to_u64` to populate the `Value::TokenAmount { amount: u64, ... }` field:

```rust
fn nat_to_u64(tokens: Nat) -> Result<u64, Icrc21Error> {
    tokens.0.to_u64().ok_or(Icrc21Error::GenericError {
        error_code: Nat::from(500_u64),
        description: "Failed to convert tokens to u64".to_owned(),
    })
}
``` [1](#0-0) 

The ICRC-1 standard defines token amounts as unbounded `Nat`. The `Value::TokenAmount` struct, however, stores `amount` as `u64`:

```rust
pub enum Value {
    TokenAmount {
        decimals: u8,
        amount: u64,
        symbol: String,
    },
    ...
}
``` [2](#0-1) 

This conversion is called unconditionally in `add_amount` for the `FieldsDisplayMessage` path: [3](#0-2) 

And similarly in `add_fee`, `add_allowance`, and `add_existing_allowance`: [4](#0-3) 

The ICRC-1 ledger's `icrc21_canister_call_consent_message` endpoint reads `decimals` directly from the ledger and passes it through to these helpers: [5](#0-4) 

### Impact Explanation

For ckETH (18 decimals), `u64::MAX` ≈ 18.44 ETH in base units. Any user requesting a `FieldsDisplay` ICRC-21 consent message for a transfer, approval, or allowance exceeding ~18.44 ckETH receives a `GenericError` response. The `icrc21_canister_call_consent_message` endpoint is an `#[update]` call used by hardware wallets and signing devices to display structured transaction details before signing. When it fails, wallets that depend on ICRC-21 for user confirmation cannot complete the signing flow for large-value transfers, degrading security UX and potentially blocking legitimate high-value transactions.

The `GenericDisplayMessage` path is unaffected because it uses `convert_tokens_to_string_representation`, which converts to `f64` (which can represent values up to ~1.8×10^308): [6](#0-5) 

Only the `FieldsDisplay` device type is broken.

### Likelihood Explanation

**Medium.** ckETH is a live mainnet token with 18 decimals. The failure threshold is ~18.44 ETH — a realistic transfer amount for institutional or DeFi users. Any ckERC20 token with 18 decimals (e.g., ckLINK, ckPEPE as shown in testnet deployments) shares the same constraint. The endpoint is publicly callable by any ingress sender without any privilege. [7](#0-6) 

### Recommendation

Replace `nat_to_u64` in the `FieldsDisplayMessage` path with a `u128`-based or `BigUint`-based representation, or cap/normalize the amount before conversion. The `Value::TokenAmount` type should be updated to use `Nat` or `u128` for `amount` to accommodate tokens with many decimals. Alternatively, fall back to `GenericDisplayMessage` when the amount exceeds `u64::MAX` rather than returning an error.

### Proof of Concept

1. Deploy an ICRC-1 ledger with `decimals = 18` (e.g., ckETH ledger).
2. Call `icrc21_canister_call_consent_message` with:
   - `method = "icrc1_transfer"`
   - `arg` encoding a `TransferArg` with `amount = 19_000_000_000_000_000_000` (19 ETH in wei, > `u64::MAX` ≈ 18.44 ETH)
   - `device_spec = FieldsDisplay`
3. Observe the response is `Err(GenericError { error_code: 500, description: "Failed to convert tokens to u64" })` instead of a valid consent message.
4. The same call with `device_spec = GenericDisplay` succeeds, confirming the bug is isolated to the `FieldsDisplay` path via `nat_to_u64`. [8](#0-7)

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

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L101-126)
```rust
    pub fn add_amount(
        &mut self,
        amount: Option<Nat>,
        decimals: u8,
        token_symbol: &String,
    ) -> Result<(), Icrc21Error> {
        let amount = amount.ok_or(Icrc21Error::GenericError {
            error_code: Nat::from(500_u64),
            description: "Amount has to be specified.".to_owned(),
        })?;
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

**File:** packages/icrc-ledger-types/src/icrc21/responses.rs (L153-157)
```rust
            ConsentMessage::FieldsDisplayMessage(fields_display) => {
                let token_amount = Value::TokenAmount {
                    decimals,
                    amount: nat_to_u64(amount)?,
                    symbol: token_symbol.to_string(),
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

**File:** rs/ethereum/cketh/testnet/README.md (L88-98)
```markdown
## Add ckSepoliaLINK

```shell
dfx deploy orchestrator --network ic --argument "(variant { AddErc20Arg = record { contract = record { chain_id = 11155111; address = \"0x779877A7B0D9E8603169DdbD7836e478b4624789\" }; ledger_init_arg = record { minting_account = record { owner = principal \"$(dfx canister --network ic id minter)\" }; feature_flags  = opt record { icrc2 = true }; decimals = opt 18; max_memo_length = opt 80; transfer_fee = 200_000_000_000_000; token_symbol = \"ckSepoliaLINK\"; token_name = \"Chain key Sepolia LINK\"; token_logo = \"\"; initial_balances = vec {}; }; git_commit_hash = \"3924e543af04d30a0b601d749721af239a10dff6\";  ledger_compressed_wasm_hash = \"57e2a728f9ffcb1a7d9e101dbd1260f8b9f3246bf5aa2ad3e2c750e125446838\"; index_compressed_wasm_hash = \"6fb62c7e9358ca5c937a5d25f55700459ed09a293d0826c09c6 ... (truncated)
```

## Add ckSepoliaPEPE

```shell
dfx deploy orchestrator --network ic --argument "(variant { AddErc20Arg = record { contract = record { chain_id = 11155111; address = \"0x560eF9F39E4B08f9693987cad307f6FBfd97B2F6\" }; ledger_init_arg = record { minting_account = record { owner = principal \"$(dfx canister --network ic id minter)\" }; feature_flags  = opt record { icrc2 = true }; decimals = opt 18; max_memo_length = opt 80; transfer_fee = 100_000_000_000_000_000_000; token_symbol = \"ckSepoliaPEPE\"; token_name = \"Chain key Sepolia PEPE\"; token_logo = \"\"; initial_balances = vec {}; }; git_commit_hash = \"3924e543af04d30a0b601d749721af239a10dff6\";  ledger_compressed_wasm_hash = \"57e2a728f9ffcb1a7d9e101dbd1260f8b9f3246bf5aa2ad3e2c750e125446838\"; index_compressed_wasm_hash = \"6fb62c7e9358ca5c937a5d25f55700459ed09a293d0 ... (truncated)
```
```
