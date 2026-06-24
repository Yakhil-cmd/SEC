### Title
Unicode Homoglyph Bypass of Banned Token Symbol/Name Check in SNS Ledger Validation - (File: `rs/nervous_system/common/src/ledger_validation.rs`)

### Summary

`validate_token_symbol` and `validate_token_name` in `rs/nervous_system/common/src/ledger_validation.rs` do not restrict input to ASCII characters. The banned-symbol check uses Rust's `str::to_uppercase()`, which for Unicode input does not produce ASCII-equivalent uppercase, allowing an unprivileged SNS proposer to register a token symbol visually indistinguishable from `"ICP"` or `"DFINITY"` by substituting Unicode homoglyphs (e.g., Greek capital iota `Ι` U+0399 for Latin `I`).

### Finding Description

`validate_token_symbol` enforces three constraints:

1. Byte length between `MIN_TOKEN_SYMBOL_LENGTH` (3) and `MAX_TOKEN_SYMBOL_LENGTH` (10)
2. No leading/trailing whitespace
3. Not in `BANNED_TOKEN_SYMBOLS = &["ICP", "DFINITY"]`, checked via `token_symbol.to_uppercase()` [1](#0-0) 

There is no ASCII-only guard. Rust's `str::to_uppercase()` on a Unicode string does not fold Unicode characters to their ASCII look-alikes. For example, Greek capital iota `Ι` (U+0399) is already uppercase; `"ΙCP".to_uppercase()` returns `"ΙCP"`, not `"ICP"`. The banned-list comparison therefore fails to match, and the symbol passes validation.

By contrast, the ckERC-20 minter's `CkTokenSymbol::from_str` explicitly rejects non-ASCII input: [2](#0-1) 

No equivalent guard exists in the SNS path.

The same flaw applies to `validate_token_name` and its `to_lowercase()` banned-name check: [3](#0-2) 

This function is called in two reachable on-chain paths:

**Path 1 – SNS creation** (`SnsInitPayload::validate_pre_execution` / `validate_post_execution`): [4](#0-3) 

**Path 2 – Post-launch symbol/name change** (`ManageLedgerParameters` SNS governance proposal): [5](#0-4) 

The `ManageLedgerParameters` proposal type is a first-class SNS governance action reachable by any SNS neuron holder with sufficient voting power: [6](#0-5) 

### Impact Explanation

An attacker who controls an SNS (or can pass an SNS governance vote) can set the on-chain `icrc1_symbol` of their ledger to `"ΙCP"` (Greek iota + Latin CP). This symbol:

- Passes all current validation checks
- Is stored and served by the SNS ledger's `icrc1_symbol` query
- Renders identically to `"ICP"` in most fonts used by wallets, DEXes, and the NNS dapp

Users querying the ledger or viewing the token in any UI that does not perform Unicode normalization will see `"ICP"` and may mistake the SNS token for the native ICP token, enabling phishing, fraudulent swap listings, and financial loss. The same applies to `"DFINITY"` and the banned token names.

### Likelihood Explanation

Any party that can submit and pass an SNS governance proposal — a realistic threshold for a newly launched SNS with concentrated neuron ownership — can exploit this. The attack requires no privileged access, no key compromise, and no off-chain infrastructure. The homoglyph characters are freely available in Unicode and trivially inserted into a Candid string argument.

### Recommendation

Add an ASCII-only guard to both `validate_token_symbol` and `validate_token_name`, mirroring the check already present in `CkTokenSymbol::from_str`:

```rust
if !token_symbol.is_ascii() {
    return Err("Token symbol must contain only ASCII characters".to_string());
}
```

Place this check before the banned-list comparison in both functions in `rs/nervous_system/common/src/ledger_validation.rs`.

### Proof of Concept

**Crafted symbol**: `"ΙCP"` — Unicode codepoints U+0399 (GREEK CAPITAL LETTER IOTA), U+0043 (C), U+0050 (P).

**Byte length**: 4 bytes (U+0399 encodes as 2 UTF-8 bytes; C and P are 1 byte each). Satisfies `3 ≤ 4 ≤ 10`. [7](#0-6) 

**Whitespace check**: No leading/trailing whitespace — passes. [8](#0-7) 

**Banned-list check**: `"ΙCP".to_uppercase()` → `"ΙCP"` (Greek iota has no ASCII uppercase mapping). `["ICP", "DFINITY"].contains(&"ΙCP")` → `false` — passes. [9](#0-8) 

**Result**: `validate_token_symbol("ΙCP")` returns `Ok(())`. The symbol is accepted, stored in the SNS ledger, and displayed to users as visually identical to `"ICP"`.

The same technique applies to `"DFIΝITY"` (Cyrillic `Ν` U+041D substituted for Latin `N`) and to the banned token names via `validate_token_name`.

### Citations

**File:** rs/nervous_system/common/src/ledger_validation.rs (L23-48)
```rust
pub fn validate_token_symbol(token_symbol: &str) -> Result<(), String> {
    if token_symbol.len() > MAX_TOKEN_SYMBOL_LENGTH {
        return Err(format!(
            "Error: token-symbol must be fewer than {} characters, given character count: {}",
            MAX_TOKEN_SYMBOL_LENGTH,
            token_symbol.len()
        ));
    }

    if token_symbol.len() < MIN_TOKEN_SYMBOL_LENGTH {
        return Err(format!(
            "Error: token-symbol must be greater than {} characters, given character count: {}",
            MIN_TOKEN_SYMBOL_LENGTH,
            token_symbol.len()
        ));
    }

    if token_symbol != token_symbol.trim() {
        return Err("Token symbol must not have leading or trailing whitespaces".to_string());
    }

    if BANNED_TOKEN_SYMBOLS.contains(&token_symbol.to_uppercase().as_ref()) {
        return Err("Banned token symbol, please chose another one.".to_string());
    }

    Ok(())
```

**File:** rs/nervous_system/common/src/ledger_validation.rs (L72-81)
```rust
    if BANNED_TOKEN_NAMES.contains(
        &token_name
            .to_lowercase()
            .chars()
            .filter(|c| !c.is_whitespace())
            .collect::<String>()
            .as_ref(),
    ) {
        return Err("Banned token name, please chose another one.".to_string());
    }
```

**File:** rs/ethereum/cketh/minter/src/erc20.rs (L63-65)
```rust
        if !token_symbol.is_ascii() {
            return Err("ERROR: token symbol contains non-ascii characters".to_string());
        }
```

**File:** rs/sns/init/src/lib.rs (L961-968)
```rust
    fn validate_token_symbol(&self) -> Result<(), String> {
        let token_symbol = self
            .token_symbol
            .as_ref()
            .ok_or_else(|| "Error: token-symbol must be specified".to_string())?;

        ledger_validation::validate_token_symbol(token_symbol)
    }
```

**File:** rs/sns/governance/src/proposal.rs (L1782-1784)
```rust
    if let Some(token_symbol) = token_symbol {
        ledger_validation::validate_token_symbol(token_symbol)?;
        render += &format!("# Set token symbol: {token_symbol}. \n",);
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L395-400)
```text
message ManageLedgerParameters {
  optional uint64 transfer_fee = 1;
  optional string token_name = 2;
  optional string token_symbol = 3;
  optional string token_logo = 4;
}
```
