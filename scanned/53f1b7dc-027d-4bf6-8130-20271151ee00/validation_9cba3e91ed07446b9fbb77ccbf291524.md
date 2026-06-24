The code is fully readable. Let me analyze the exact logic carefully.

### Title
Unicode Internal Whitespace Bypass in `validate_token_symbol` Allows Visual Impersonation of Banned Symbols — (`rs/nervous_system/common/src/ledger_validation.rs`)

---

### Summary

`validate_token_symbol` only checks for leading/trailing whitespace via `trim()` and compares the raw uppercased string against the banned list. It does not strip internal Unicode whitespace characters before the banned-symbol comparison. This is directly contradicted by the sibling function `validate_token_name`, which explicitly filters all whitespace before comparing. A token symbol like `"I\u{200B}C\u{200B}P"` passes every check and is visually indistinguishable from `"ICP"` in any UI.

---

### Finding Description

In `rs/nervous_system/common/src/ledger_validation.rs`, `validate_token_symbol` performs three checks:

1. **Byte-length bounds** (lines 24–38): `token_symbol.len()` uses UTF-8 byte count. `"I\u{200B}C\u{200B}P"` is 9 bytes (U+200B encodes to 3 bytes `0xE2 0x80 0x8B`), within the 3–10 byte limit.

2. **Leading/trailing whitespace** (line 40): `token_symbol != token_symbol.trim()`. Rust's `str::trim()` only strips leading and trailing characters where `char::is_whitespace()` is true. U+200B (ZERO WIDTH SPACE) does **not** have the Unicode `White_Space` property in Unicode 6.3+, so `trim()` does not remove it at all — not even from the edges. The check passes trivially.

3. **Banned symbol check** (line 44): `BANNED_TOKEN_SYMBOLS.contains(&token_symbol.to_uppercase().as_ref())`. `"I\u{200B}C\u{200B}P".to_uppercase()` yields `"I\u{200B}C\u{200B}P"`, which is not equal to `"ICP"`. The check passes. [1](#0-0) 

The **critical asymmetry** is that `validate_token_name` (lines 72–79) already applies the correct fix — it strips all whitespace with `.filter(|c| !c.is_whitespace())` before comparing against banned names — but `validate_token_symbol` does not: [2](#0-1) 

This function is called in two production paths:

- **SNS creation** (`SnsInitPayload::validate_pre_execution` and `validate_post_execution`), invoked by NNS governance during `CreateServiceNervousSystem` proposal validation: [3](#0-2) [4](#0-3) 

- **Post-launch symbol change** via SNS governance `ManageLedgerParameters` proposals: [5](#0-4) 

---

### Impact Explanation

An SNS token with symbol `"I\u{200B}C\u{200B}P"` is stored and displayed as visually identical to `"ICP"` in every UI that renders the symbol string (wallets, DEXes, dashboards). Zero-width spaces are invisible in all standard font renderers. Users cannot distinguish the impersonating token from the real ICP token by visual inspection of the symbol alone. This enables phishing, fraudulent swap listings, and confusion in ledger UIs.

---

### Likelihood Explanation

The `CreateServiceNervousSystem` path requires an NNS governance proposal to pass. However:
- Any principal with a staked NNS neuron can submit the proposal.
- The proposal rendering at line 1784 outputs the raw symbol string; NNS voters viewing the proposal on the dashboard would see `"ICP"` with no visible indication of embedded invisible characters.
- The `ManageLedgerParameters` path requires only SNS governance approval, which is controlled by the SNS creator's own neuron allocation at launch.

The governance requirement reduces but does not eliminate likelihood. The invisible-character technique is specifically designed to evade human review. [6](#0-5) 

---

### Recommendation

Apply the same whitespace-stripping approach already used in `validate_token_name` to `validate_token_symbol`:

```rust
// Before banned-symbol check, strip all Unicode whitespace:
let normalized = token_symbol
    .chars()
    .filter(|c| !c.is_whitespace())
    .collect::<String>();

if BANNED_TOKEN_SYMBOLS.contains(&normalized.to_uppercase().as_ref()) {
    return Err("Banned token symbol, please chose another one.".to_string());
}
```

Additionally, consider rejecting any symbol that contains non-ASCII or non-printable characters entirely, since token symbols have no legitimate use for Unicode whitespace. [7](#0-6) 

---

### Proof of Concept

```rust
#[test]
fn test_zwsp_bypass() {
    // "ICP" with zero-width spaces between each character
    let symbol = "I\u{200B}C\u{200B}P";
    // Byte length = 9, within 3..=10 ✓
    assert_eq!(symbol.len(), 9);
    // trim() does not remove U+200B (not Unicode White_Space) ✓
    assert_eq!(symbol, symbol.trim());
    // to_uppercase() preserves U+200B, does not equal "ICP" ✓
    assert_ne!(symbol.to_uppercase(), "ICP");
    // Current validate_token_symbol returns Ok — BUG
    assert_eq!(validate_token_symbol(symbol), Ok(()));
    // Visually identical to "ICP" in any renderer
}
```

### Citations

**File:** rs/nervous_system/common/src/ledger_validation.rs (L17-48)
```rust
/// Token Symbols that can not be used.
const BANNED_TOKEN_SYMBOLS: &[&str] = &["ICP", "DFINITY"];

/// Token Names that can not be used.
const BANNED_TOKEN_NAMES: &[&str] = &["internetcomputer", "internetcomputerprotocol"];

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

**File:** rs/nervous_system/common/src/ledger_validation.rs (L72-79)
```rust
    if BANNED_TOKEN_NAMES.contains(
        &token_name
            .to_lowercase()
            .chars()
            .filter(|c| !c.is_whitespace())
            .collect::<String>()
            .as_ref(),
    ) {
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

**File:** rs/nns/governance/src/governance.rs (L5037-5051)
```rust
    fn validate_create_service_nervous_system(
        &self,
        create_service_nervous_system: &CreateServiceNervousSystem,
    ) -> Result<(), GovernanceError> {
        // Must be able to convert to a valid SnsInitPayload.
        let conversion_result = SnsInitPayload::try_from(ApiCreateServiceNervousSystem::from(
            create_service_nervous_system.clone(),
        ));

        let validated = conversion_result.map_err(|e| {
            GovernanceError::new_with_message(
                ErrorType::InvalidProposal,
                format!("Invalid CreateServiceNervousSystem: {e}"),
            )
        })?;
```

**File:** rs/sns/governance/src/proposal.rs (L1782-1786)
```rust
    if let Some(token_symbol) = token_symbol {
        ledger_validation::validate_token_symbol(token_symbol)?;
        render += &format!("# Set token symbol: {token_symbol}. \n",);
        change = true;
    }
```
