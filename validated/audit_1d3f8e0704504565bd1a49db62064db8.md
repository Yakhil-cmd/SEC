The code is clear. Let me verify the Unicode behavior claim precisely before rendering a verdict.

### Title
Unicode Homoglyph Bypass in `validate_token_symbol` Allows SNS Token to Impersonate ICP Ticker — (`rs/nervous_system/common/src/ledger_validation.rs`)

---

### Summary

`validate_token_symbol` bans `"ICP"` and `"DFINITY"` by comparing `token_symbol.to_uppercase()` against an ASCII-only list. Rust's Unicode-aware `to_uppercase()` does **not** map Cyrillic 'С' (U+0421) to Latin 'C' (U+0043), so a symbol like `"IСP"` (Cyrillic С in position 2) uppercases to `"IСP"` — which is not in `BANNED_TOKEN_SYMBOLS` — and passes validation. The resulting SNS token renders identically to `"ICP"` in virtually all standard fonts.

---

### Finding Description

The guard is at line 44:

```rust
if BANNED_TOKEN_SYMBOLS.contains(&token_symbol.to_uppercase().as_ref()) {
    return Err("Banned token symbol, please chose another one.".to_string());
}
``` [1](#0-0) 

`BANNED_TOKEN_SYMBOLS` contains only the ASCII strings `"ICP"` and `"DFINITY"`. [2](#0-1) 

Rust's `str::to_uppercase()` follows Unicode case-folding rules. Cyrillic capital С (U+0421) uppercases to itself (U+0421), not to Latin C (U+0043). Therefore `"IСP".to_uppercase() == "IСP"`, which is not equal to `"ICP"`, and `BANNED_TOKEN_SYMBOLS.contains(...)` returns `false`.

The call chain from proposal submission to deployment:

1. Unprivileged NNS neuron holder submits `CreateServiceNervousSystem` with `token_symbol = "IСP"` (Cyrillic С).
2. `validate_create_service_nervous_system` → `SnsInitPayload::try_from` → `validate_pre_execution` → `validate_token_symbol` → **returns `Ok(())`**. [3](#0-2) 
3. Proposal is open for NNS voting. The rendered proposal text shows `"IСP"` — visually indistinguishable from `"ICP"` in the NNS dapp.
4. On adoption, `execute_create_service_nervous_system_proposal` → `validate_post_execution` → `validate_token_symbol` → **still returns `Ok(())`**. [4](#0-3) 
5. SNS is deployed with ledger symbol `"IСP"`. [5](#0-4) 

The same bypass applies to `ManageLedgerParameters` proposals on an existing SNS, which also call `validate_token_symbol` without any additional guard. [6](#0-5) 

---

### Impact Explanation

An SNS token with symbol `"IСP"` (Cyrillic С) is visually identical to the ICP ticker in all standard fonts and in the NNS/SNS dashboards. Users who see this symbol in swap UIs, wallets, or DEX listings cannot distinguish it from the real ICP token without inspecting raw bytes or canister IDs. This enables:

- **Phishing/swap fraud**: users sell real ICP for the fake `"IСP"` SNS token, or vice versa, believing they are trading ICP.
- **Reputation damage** to the ICP ecosystem.

The impact is **not** direct illegal minting of ICP (the SNS token is a separate ledger). The financial harm is indirect — through user deception — but is realistic and potentially large given ICP's market cap.

---

### Likelihood Explanation

The technical barrier is low: any NNS neuron holder can submit the proposal. The governance barrier is real but not a complete mitigation — NNS neurons vote on rendered text where the homoglyph is invisible. The bypass is trivially reproducible with a one-line unit test. No privileged access, key material, or majority corruption is required.

---

### Recommendation

Replace the `to_uppercase()` comparison with an ASCII-only check:

```rust
if !token_symbol.is_ascii() {
    return Err("Token symbol must contain only ASCII characters.".to_string());
}
if BANNED_TOKEN_SYMBOLS.contains(&token_symbol.to_uppercase().as_ref()) {
    return Err("Banned token symbol, please chose another one.".to_string());
}
```

Rejecting non-ASCII symbols entirely is the simplest and most robust fix. It also closes the same class of bypass for `BANNED_TOKEN_NAMES` (which uses `to_lowercase()`). [7](#0-6) 

---

### Proof of Concept

```rust
#[test]
fn homoglyph_bypass() {
    // "IСP" — position 1 is Cyrillic С (U+0421), not Latin C (U+0043)
    let fake_icp = "I\u{0421}P";
    assert_eq!(fake_icp.to_uppercase(), "I\u{0421}P"); // does NOT become "ICP"
    // Passes validation today:
    assert!(validate_token_symbol(fake_icp).is_ok());
}
```

This test passes against the current production code, confirming the bypass is concrete and local-testable.

### Citations

**File:** rs/nervous_system/common/src/ledger_validation.rs (L17-49)
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
}
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
