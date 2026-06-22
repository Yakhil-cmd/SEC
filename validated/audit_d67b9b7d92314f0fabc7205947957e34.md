### Title
Insufficient Character-Set Validation in SNS Token Symbol and Name Registration Allows Unicode Homoglyph Impersonation - (File: `rs/nervous_system/common/src/ledger_validation.rs`)

### Summary
`validate_token_symbol` and `validate_token_name` in `rs/nervous_system/common/src/ledger_validation.rs` enforce only length bounds and a hardcoded ASCII banned-list. They impose no character-set restriction, so any unprivileged user can register an SNS whose token symbol or name is visually indistinguishable from a protected identifier (e.g., "ICP", "DFINITY") by substituting Unicode homoglyphs. The same weak validators are reused by the `ManageLedgerParameters` and `ManageSnsMetadata` SNS governance proposal paths, so the attack surface extends to post-deployment token renaming as well.

### Finding Description

`validate_token_symbol` performs three checks and then returns `Ok`:

```
length ∈ [3, 10]
no leading/trailing ASCII whitespace
to_uppercase() ∉ {"ICP", "DFINITY"}
``` [1](#0-0) 

`validate_token_name` is analogous:

```
length ∈ [4, 255]
no leading/trailing ASCII whitespace
to_lowercase().filter(!whitespace) ∉ {"internetcomputer", "internetcomputerprotocol"}
``` [2](#0-1) 

Neither function restricts the Unicode code-point set. The banned-list comparison uses Rust's `str::to_uppercase` / `str::to_lowercase`, which performs Unicode case-folding on each code point independently but does **not** perform Unicode normalization or script-mixing detection. Consequently, a string composed entirely of Cyrillic lookalikes — e.g., `"ІСР"` (U+0406 CYRILLIC CAPITAL LETTER BYELORUSSIAN-UKRAINIAN I, U+0421 CYRILLIC CAPITAL LETTER ES, U+0420 CYRILLIC CAPITAL LETTER ER) — uppercases to `"ІСР"`, which is not equal to the ASCII string `"ICP"`, so the ban is bypassed.

The same validators are called from two additional on-chain proposal paths:

- `validate_and_render_manage_ledger_parameters` (SNS governance `ManageLedgerParameters` proposal) calls `ledger_validation::validate_token_symbol` and `ledger_validation::validate_token_name`. [3](#0-2) 

- `validate_and_render_manage_sns_metadata` (SNS governance `ManageSnsMetadata` proposal) calls `SnsMetadata::validate_name`, which only checks length. [4](#0-3) [5](#0-4) 

The `SnsInitPayload::validate_pre_execution` and `validate_post_execution` methods, called by NNS governance before executing a `CreateServiceNervousSystem` proposal, delegate to these same functions. [6](#0-5) 

### Impact Explanation

An attacker deploys an SNS whose ICRC-1 ledger carries the token symbol `"ІСР"` (Cyrillic, visually identical to `"ICP"` in most fonts). Every wallet, DEX, and explorer that renders the symbol string will display what appears to be `"ICP"`. Users who rely on the displayed symbol to identify the asset can be deceived into:

- Sending real ICP to the attacker's SNS swap canister believing they are participating in a legitimate ICP-denominated swap.
- Approving ICRC-1 `transfer_from` allowances on the wrong ledger.
- Accepting the homoglyph token as payment in place of the genuine ICP token.

The same technique applies to impersonating any well-known SNS token (e.g., `"SNS1"`, `"CHAT"`) by substituting one or more lookalike code points. Post-deployment, an SNS that initially launched with a legitimate symbol can rename itself via a `ManageLedgerParameters` governance proposal to a homoglyph of a competing project's symbol, since the same validator is reused.

### Likelihood Explanation

The attack requires only that the attacker stake enough ICP to submit an NNS proposal (currently 1 ICP) and that the proposal passes a governance vote. Because the NNS processes `CreateServiceNervousSystem` proposals routinely and the malicious payload is syntactically valid, the proposal is indistinguishable from a legitimate SNS launch at the validation layer. The homoglyph substitution is a well-known, low-effort technique. No privileged access, key material, or subnet-majority corruption is required.

### Recommendation

Restrict the allowed character set in both `validate_token_symbol` and `validate_token_name` to a safe subset. The minimal fix mirrors the approach taken in the ZNS fix: accept only printable ASCII (`[0x20-0x7E]`) or, more strictly, alphanumeric ASCII plus a small set of punctuation. Additionally, apply Unicode NFKC normalization before the banned-list comparison so that visually equivalent strings are collapsed before the check.

```rust
// Example addition to validate_token_symbol
if !token_symbol.chars().all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_') {
    return Err("Token symbol must contain only ASCII alphanumeric characters, hyphens, or underscores.".to_string());
}
```

The same guard should be applied in `SnsMetadata::validate_name` and `validate_token_name`.

### Proof of Concept

1. Construct a `CreateServiceNervousSystem` NNS proposal with `ledger_parameters.token_symbol = "ІСР"` (three Cyrillic code points U+0406, U+0421, U+0420).
2. Submit the proposal from any NNS neuron with sufficient dissolve delay.
3. Observe that `validate_token_symbol("ІСР")` returns `Ok(())`:
   - `"ІСР".len()` = 6 bytes (each Cyrillic char is 2 UTF-8 bytes) — within `[3, 10]`. ✓
   - `"ІСР" != "ІСР".trim()` is false (no whitespace). ✓
   - `"ІСР".to_uppercase()` = `"ІСР"` ≠ `"ICP"`. ✓ (ban bypassed)
4. After the proposal executes, the deployed SNS ledger advertises `icrc1_symbol() → "ІСР"`, which renders as `ICP` in any font that maps Cyrillic lookalikes to the same glyph. [7](#0-6) [8](#0-7)

### Citations

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

**File:** rs/nervous_system/common/src/ledger_validation.rs (L51-84)
```rust
pub fn validate_token_name(token_name: &str) -> Result<(), String> {
    if token_name.len() > MAX_TOKEN_NAME_LENGTH {
        return Err(format!(
            "Error: token-name must be fewer than {} characters, given character count: {}",
            MAX_TOKEN_NAME_LENGTH,
            token_name.len()
        ));
    }

    if token_name.len() < MIN_TOKEN_NAME_LENGTH {
        return Err(format!(
            "Error: token-name must be greater than {} characters, given character count: {}",
            MIN_TOKEN_NAME_LENGTH,
            token_name.len()
        ));
    }

    if token_name != token_name.trim() {
        return Err("Token name must not have leading or trailing whitespaces".to_string());
    }

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

    Ok(())
}
```

**File:** rs/sns/governance/src/proposal.rs (L1736-1739)
```rust
    if let Some(new_name) = &manage_sns_metadata.name {
        SnsMetadata::validate_name(new_name)?;
        render += &format!("# New name: {new_name} \n");
        no_change = false;
```

**File:** rs/sns/governance/src/proposal.rs (L1777-1785)
```rust
    if let Some(token_name) = token_name {
        ledger_validation::validate_token_name(token_name)?;
        render += &format!("# Set token name: {token_name}. \n",);
        change = true;
    }
    if let Some(token_symbol) = token_symbol {
        ledger_validation::validate_token_symbol(token_symbol)?;
        render += &format!("# Set token symbol: {token_symbol}. \n",);
        change = true;
```

**File:** rs/sns/governance/src/types.rs (L1698-1711)
```rust
    pub fn validate_name(name: &str) -> Result<(), String> {
        if name.len() > Self::MAX_NAME_LENGTH {
            return Err(format!(
                "SnsMetadata.name must be less than {} characters",
                Self::MAX_NAME_LENGTH
            ));
        } else if name.len() < Self::MIN_NAME_LENGTH {
            return Err(format!(
                "SnsMetadata.name must be greater than {} characters",
                Self::MIN_NAME_LENGTH
            ));
        }
        Ok(())
    }
```

**File:** rs/sns/init/src/lib.rs (L849-851)
```rust
        let validation_fns = [
            self.validate_token_symbol(),
            self.validate_token_name(),
```

**File:** rs/sns/init/src/lib.rs (L961-967)
```rust
    fn validate_token_symbol(&self) -> Result<(), String> {
        let token_symbol = self
            .token_symbol
            .as_ref()
            .ok_or_else(|| "Error: token-symbol must be specified".to_string())?;

        ledger_validation::validate_token_symbol(token_symbol)
```
