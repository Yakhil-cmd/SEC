### Title
Unicode Homoglyph Bypass in `validate_token_symbol` Allows SNS Token Symbol Visually Identical to "ICP" — (`rs/nervous_system/common/src/ledger_validation.rs`)

---

### Summary

`validate_token_symbol` uses Rust's `str::to_uppercase()` to compare against the banned list `["ICP", "DFINITY"]`. Because `to_uppercase()` applies Unicode case mapping (not normalization), Cyrillic homoglyphs that are already uppercase (e.g., І U+0406, С U+0421, Р U+0420) remain as Cyrillic after the call. The result `"ІСР"` ≠ `"ICP"`, so the check silently passes. An SNS creator who controls their own SNS governance majority can submit a `ManageLedgerParameters` proposal setting `token_symbol` to such a string, producing a live ledger whose `icrc1_symbol()` returns a value visually indistinguishable from "ICP" in virtually all fonts.

---

### Finding Description

The banned-symbol guard is:

```rust
const BANNED_TOKEN_SYMBOLS: &[&str] = &["ICP", "DFINITY"];

if BANNED_TOKEN_SYMBOLS.contains(&token_symbol.to_uppercase().as_ref()) {
    return Err("Banned token symbol, please chose another one.".to_string());
}
``` [1](#0-0) [2](#0-1) 

`str::to_uppercase()` in Rust performs Unicode case mapping per character. The Cyrillic letters І (U+0406), С (U+0421), Р (U+0420) are already in their uppercase form; their Unicode uppercase mapping is themselves. Therefore:

- Input: `"ІСР"` (Cyrillic, 3 bytes each = 9 bytes total, within the 10-char byte limit)
- `"ІСР".to_uppercase()` → `"ІСР"` (unchanged, still Cyrillic)
- `BANNED_TOKEN_SYMBOLS.contains(&"ІСР")` → `false`
- `validate_token_symbol` returns `Ok(())`

The same function is called from both `SnsInitPayload::validate_pre_execution` / `validate_post_execution` (for `CreateServiceNervousSystem`) and from `validate_and_render_manage_ledger_parameters` (for `ManageLedgerParameters`): [3](#0-2) [4](#0-3) 

There is no additional Unicode normalization or script-restriction check anywhere in the call chain.

---

### Impact Explanation

A successfully deployed SNS ledger with symbol `"ІСР"` will respond to `icrc1_symbol()` with that Cyrillic string. Every wallet, DEX, and dashboard that renders it will display what appears to be `ICP`. Users transferring funds to or from such a token would have no visual indication it is not the native ICP token, enabling large-scale phishing and theft.

---

### Likelihood Explanation

The `ManageLedgerParameters` path is the realistic vector: an SNS creator who retains a governance majority in their own SNS (common at launch, when developer neurons hold the majority of voting power) can unilaterally pass this proposal post-deployment. The `CreateServiceNervousSystem` path additionally requires NNS governance approval, which is a higher bar. The `ManageLedgerParameters` path requires only SNS-internal governance majority, which the creator typically holds. [5](#0-4) 

---

### Recommendation

Replace the `to_uppercase()` comparison with a Unicode-normalization-aware check. At minimum, reject any `token_symbol` that contains non-ASCII characters, or apply NFKC normalization before comparison. A simple guard:

```rust
if !token_symbol.is_ascii() {
    return Err("Token symbol must contain only ASCII characters.".to_string());
}
if BANNED_TOKEN_SYMBOLS.contains(&token_symbol.to_uppercase().as_ref()) {
    return Err("Banned token symbol, please chose another one.".to_string());
}
```

This closes the homoglyph class entirely. Alternatively, use a Unicode confusables database (Unicode TR#39) to detect visually confusable strings. [6](#0-5) 

---

### Proof of Concept

```rust
#[test]
fn cyrillic_homoglyph_bypasses_banned_symbol_check() {
    // Cyrillic І (U+0406) + С (U+0421) + Р (U+0420)
    // Visually identical to Latin "ICP" in most fonts
    let homoglyph = "\u{0406}\u{0421}\u{0420}";
    assert_eq!(homoglyph.to_uppercase(), homoglyph); // stays Cyrillic
    assert_ne!(homoglyph.to_uppercase().as_str(), "ICP"); // not caught
    // This currently returns Ok(()):
    assert!(validate_token_symbol(homoglyph).is_ok());
}
```

This unit test, runnable against the current production code in `rs/nervous_system/common/src/ledger_validation.rs`, demonstrates the bypass concretely and locally without any network access. [2](#0-1)

### Citations

**File:** rs/nervous_system/common/src/ledger_validation.rs (L18-18)
```rust
const BANNED_TOKEN_SYMBOLS: &[&str] = &["ICP", "DFINITY"];
```

**File:** rs/nervous_system/common/src/ledger_validation.rs (L23-49)
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
}
```

**File:** rs/sns/init/src/lib.rs (L850-851)
```rust
            self.validate_token_symbol(),
            self.validate_token_name(),
```

**File:** rs/sns/governance/src/proposal.rs (L1761-1799)
```rust
fn validate_and_render_manage_ledger_parameters(
    manage_ledger_parameters: &ManageLedgerParameters,
) -> Result<String, String> {
    let mut change = false;
    let mut render = "# Proposal to change ledger parameters:\n".to_string();
    let ManageLedgerParameters {
        transfer_fee,
        token_name,
        token_symbol,
        token_logo,
    } = manage_ledger_parameters;

    if let Some(transfer_fee) = transfer_fee {
        render += &format!("# Set token transfer fee: {transfer_fee} token-quantums. \n",);
        change = true;
    }
    if let Some(token_name) = token_name {
        ledger_validation::validate_token_name(token_name)?;
        render += &format!("# Set token name: {token_name}. \n",);
        change = true;
    }
    if let Some(token_symbol) = token_symbol {
        ledger_validation::validate_token_symbol(token_symbol)?;
        render += &format!("# Set token symbol: {token_symbol}. \n",);
        change = true;
    }
    if let Some(token_logo) = token_logo {
        ledger_validation::validate_token_logo(token_logo)?;
        render += &format!("# Set token logo: {token_logo}. \n",);
        change = true;
    }
    if !change {
        Err(String::from(
            "ManageLedgerParameters must change at least one value, all values are None",
        ))
    } else {
        Ok(render)
    }
}
```
