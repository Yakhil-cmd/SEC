The code is fully readable. Let me verify the exact Rust semantics for `.len()` and `.to_uppercase()` on the relevant string types to confirm the claim before rendering a verdict.

### Title
Unicode Homoglyph Bypass of `BANNED_TOKEN_SYMBOLS` in `validate_token_symbol` — (`rs/nervous_system/common/src/ledger_validation.rs`)

---

### Summary

`validate_token_symbol` uses Rust's `.len()` (byte count) for length bounds and `.to_uppercase()` for ban-list comparison. Because `.to_uppercase()` on Cyrillic characters returns Cyrillic uppercase — not Latin — the ban check against `["ICP", "DFINITY"]` is trivially bypassed with visually-identical Unicode homoglyphs. The length check does not prevent this because the homoglyph strings are short in bytes.

---

### Finding Description

The function has two independent checks:

**Length check** — uses byte count: [1](#0-0) 

**Ban check** — compares `.to_uppercase()` result against ASCII-only strings: [2](#0-1) 

The banned list contains only ASCII strings: [3](#0-2) 

**Concrete bypass with Cyrillic homoglyphs:**

| Input | Codepoints | UTF-8 bytes | `.to_uppercase()` | Ban check result |
|---|---|---|---|---|
| `ICP` (Latin) | U+0049 U+0043 U+0050 | 3 | `"ICP"` | **Rejected** |
| `ІСР` (Cyrillic) | U+0406 U+0421 U+0420 | 6 | `"ІСР"` (Cyrillic) | **Passes** |

- `"ІСР".len()` = **6** → passes `> 10` check ✓  
- `"ІСР".len()` = **6** → passes `< 3` check ✓  
- `"ІСР".to_uppercase()` = `"ІСР"` ≠ `"ICP"` → passes ban check ✓  
- Visually: `І` (U+0406) ≡ `I`, `С` (U+0421) ≡ `C`, `Р` (U+0420) ≡ `P` in virtually all fonts.

The same bypass applies to `DFINITY` using Cyrillic/Greek lookalikes for D, F, I, N, T, Y.

---

### Impact Explanation

An SNS deployed with token symbol `ІСР` (Cyrillic) is visually indistinguishable from the real ICP token in wallets, DEX UIs, and governance dashboards that render the symbol as a string. Users could:

- Mistake the SNS token for ICP and send real ICP to swap contracts expecting to receive ICP
- Be deceived in secondary market listings where the symbol is the primary identifier
- Lose funds through confusion in any UI that displays the token symbol without Unicode normalization

The `validate_token_symbol` function is also called on the `ManageLedgerParameters` path, meaning an existing SNS could rename its symbol post-deployment: [4](#0-3) 

---

### Likelihood Explanation

SNS creation requires an NNS `CreateServiceNervousSystem` proposal to be approved by NNS voters: [5](#0-4) 

Only NNS Governance can call `deploy_new_sns`: [6](#0-5) 

This is a meaningful barrier. However:
- Any NNS neuron holder can submit a proposal (low cost)
- NNS governance UIs render the token symbol as a string; voters are unlikely to inspect raw Unicode codepoints
- The Cyrillic characters are visually pixel-perfect matches for Latin I, C, P in common fonts
- The validation code is the designated technical guard — its failure means the human review process is the only remaining control, and it is defeatable by visual deception

---

### Recommendation

Replace the ban check with Unicode normalization before comparison. Use NFKD/NFC normalization and/or confusable-character detection (Unicode TR#39) before comparing against the ban list. At minimum, reject any token symbol containing non-ASCII characters, or use `.chars().count()` for length and normalize to ASCII-equivalent before ban-list comparison:

```rust
// Reject non-ASCII entirely, or normalize before ban check
if !token_symbol.is_ascii() {
    return Err("Token symbol must contain only ASCII characters.".to_string());
}
if BANNED_TOKEN_SYMBOLS.contains(&token_symbol.to_uppercase().as_ref()) {
    return Err("Banned token symbol, please choose another one.".to_string());
}
```

Also fix the length check to use `.chars().count()` (character count) instead of `.len()` (byte count) to match the comment "number of characters": [7](#0-6) 

---

### Proof of Concept

```rust
use ic_nervous_system_common::ledger_validation::validate_token_symbol;

fn main() {
    // Latin ICP — correctly rejected
    assert!(validate_token_symbol("ICP").is_err());

    // Cyrillic ІСР (U+0406, U+0421, U+0420) — visually identical, incorrectly accepted
    let cyrillic_icp = "\u{0406}\u{0421}\u{0420}"; // "ІСР"
    assert_eq!(cyrillic_icp.len(), 6);              // 6 bytes, passes length check
    assert_eq!(cyrillic_icp.to_uppercase(), cyrillic_icp); // stays Cyrillic, passes ban check
    assert!(validate_token_symbol(cyrillic_icp).is_ok()); // BUG: passes validation
    
    println!("Bypass confirmed: '{}' accepted as token symbol", cyrillic_icp);
    // Renders as: Bypass confirmed: 'ICP' accepted as token symbol
}
```

### Citations

**File:** rs/nervous_system/common/src/ledger_validation.rs (L1-5)
```rust
/// The maximum number of characters allowed for token symbol.
pub const MAX_TOKEN_SYMBOL_LENGTH: usize = 10;

/// The minimum number of characters allowed for token symbol.
pub const MIN_TOKEN_SYMBOL_LENGTH: usize = 3;
```

**File:** rs/nervous_system/common/src/ledger_validation.rs (L18-18)
```rust
const BANNED_TOKEN_SYMBOLS: &[&str] = &["ICP", "DFINITY"];
```

**File:** rs/nervous_system/common/src/ledger_validation.rs (L24-29)
```rust
    if token_symbol.len() > MAX_TOKEN_SYMBOL_LENGTH {
        return Err(format!(
            "Error: token-symbol must be fewer than {} characters, given character count: {}",
            MAX_TOKEN_SYMBOL_LENGTH,
            token_symbol.len()
        ));
```

**File:** rs/nervous_system/common/src/ledger_validation.rs (L44-46)
```rust
    if BANNED_TOKEN_SYMBOLS.contains(&token_symbol.to_uppercase().as_ref()) {
        return Err("Banned token symbol, please chose another one.".to_string());
    }
```

**File:** rs/sns/governance/src/proposal.rs (L1782-1784)
```rust
    if let Some(token_symbol) = token_symbol {
        ledger_validation::validate_token_symbol(token_symbol)?;
        render += &format!("# Set token symbol: {token_symbol}. \n",);
```

**File:** rs/nns/governance/src/governance.rs (L4519-4525)
```rust
        // Step 1.2: Validate the SnsInitPayload.
        sns_init_payload.validate_post_execution().map_err(|err| {
            GovernanceError::new_with_message(
                ErrorType::InvalidProposal,
                format!("Failed to validate SnsInitPayload: {err}"),
            )
        })?;
```

**File:** rs/nns/sns-wasm/src/sns_wasm.rs (L763-767)
```rust
        if caller != GOVERNANCE_CANISTER_ID.get() {
            return DeployNewSnsResponse::from(validation_deploy_error(
                "Only the NNS Governance may deploy a new SNS instance.".to_string(),
            ));
        }
```
