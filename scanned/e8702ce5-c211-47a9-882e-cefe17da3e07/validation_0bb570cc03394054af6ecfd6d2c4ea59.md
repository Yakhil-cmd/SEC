### Title
Bytes-vs-Characters Confusion in SNS Token Symbol/Name Validation Allows Minimum-Length Bypass and Incorrect Maximum Rejection - (File: rs/nervous_system/common/src/ledger_validation.rs)

### Summary

`validate_token_symbol` and `validate_token_name` in `rs/nervous_system/common/src/ledger_validation.rs` use Rust's `.len()` (which returns **byte count**) to enforce limits that are documented and error-reported as **character** counts. For multi-byte UTF-8 characters (emojis, CJK, etc.), this causes two symmetric failures: the minimum-length check can be bypassed with a single multi-byte character, and the maximum-length check incorrectly rejects valid symbols/names whose byte length exceeds the limit even though their character count does not.

### Finding Description

The constants and their documentation state character limits:

```rust
/// The maximum number of characters allowed for token symbol.
pub const MAX_TOKEN_SYMBOL_LENGTH: usize = 10;
/// The minimum number of characters allowed for token symbol.
pub const MIN_TOKEN_SYMBOL_LENGTH: usize = 3;
/// The maximum number of characters allowed for token name.
pub const MAX_TOKEN_NAME_LENGTH: usize = 255;
/// The minimum number of characters allowed for token name.
pub const MIN_TOKEN_NAME_LENGTH: usize = 4;
```

But the validation uses `.len()` (bytes):

```rust
pub fn validate_token_symbol(token_symbol: &str) -> Result<(), String> {
    if token_symbol.len() > MAX_TOKEN_SYMBOL_LENGTH {   // bytes, not chars
        ...
        "given character count: {}", token_symbol.len() // misleadingly reports bytes
    }
    if token_symbol.len() < MIN_TOKEN_SYMBOL_LENGTH {   // bytes, not chars
``` [1](#0-0) 

The same pattern applies to `validate_token_name`: [2](#0-1) 

**Minimum-length bypass (more impactful direction):**

- `MIN_TOKEN_SYMBOL_LENGTH = 3` (documented as characters)
- A symbol consisting of a single emoji (e.g., `🚀`, 4 bytes) passes: `4 >= 3` → accepted, despite having only 1 character
- A symbol consisting of a single CJK character (3 bytes) passes: `3 >= 3` → accepted, despite having only 1 character
- For `MIN_TOKEN_NAME_LENGTH = 4`: a single emoji (4 bytes) passes: `4 >= 4` → accepted, despite having only 1 character

**Maximum-length false rejection (opposite direction):**

- `MAX_TOKEN_SYMBOL_LENGTH = 10` (documented as characters)
- A symbol with 3 emoji characters (12 bytes) is rejected: `12 > 10` → rejected, despite having only 3 characters
- A symbol with 4 CJK characters (12 bytes) is rejected: `12 > 10` → rejected, despite having only 4 characters

These functions are called during SNS initialization validation and during `ManageLedgerParameters` proposal validation: [3](#0-2) [4](#0-3) 

### Impact Explanation

**Minimum bypass:** An SNS creator can register a token symbol consisting of a single emoji or CJK character (e.g., `🚀`), bypassing the stated 3-character minimum. This violates the governance invariant that token symbols must be at least 3 characters long, potentially creating confusion with other tokens or circumventing quality requirements enforced by the NNS/SNS governance process.

**Maximum false rejection:** Legitimate SNS creators who want to use multi-byte Unicode characters in their token symbol or name (e.g., 4 CJK characters for an Asian-market token) are incorrectly rejected even though their symbol is within the character limit. The error message compounds the confusion by reporting the byte count as "character count."

The error message itself is misleading in both cases, reporting `.len()` (bytes) as "character count": [5](#0-4) 

### Likelihood Explanation

Any unprivileged user submitting an SNS initialization payload or a `ManageLedgerParameters` governance proposal with a multi-byte Unicode token symbol or name will trigger this. The SNS creation flow is publicly accessible. The minimum-length bypass requires only knowledge that a single emoji has more than 3 bytes.

### Recommendation

Replace `.len()` (byte count) with `.chars().count()` (Unicode scalar value count) in both `validate_token_symbol` and `validate_token_name`, consistent with how `validate_chars_count` is correctly implemented elsewhere in the codebase: [6](#0-5) 

The fix:
```rust
// Before (bytes):
if token_symbol.len() > MAX_TOKEN_SYMBOL_LENGTH {

// After (characters):
if token_symbol.chars().count() > MAX_TOKEN_SYMBOL_LENGTH {
```

Also update the error messages to report `token_symbol.chars().count()` instead of `token_symbol.len()`.

### Proof of Concept

```rust
use ic_nervous_system_common::ledger_validation::{validate_token_symbol, validate_token_name};

fn main() {
    // Single emoji = 4 bytes, 1 character.
    // MIN_TOKEN_SYMBOL_LENGTH = 3 (documented as characters).
    // Passes because 4 >= 3 (bytes), but should fail (1 < 3 characters).
    let result = validate_token_symbol("🚀");
    assert!(result.is_ok(), "Single emoji bypasses 3-char minimum: {:?}", result);

    // 3 emoji = 12 bytes, 3 characters.
    // MAX_TOKEN_SYMBOL_LENGTH = 10 (documented as characters).
    // Fails because 12 > 10 (bytes), but should pass (3 <= 10 characters).
    let result = validate_token_symbol("🚀🌟💎");
    assert!(result.is_err(), "3 emoji incorrectly rejected: {:?}", result);
    // Error message will say "character count: 12" but 12 is the byte count.

    // Single emoji name = 4 bytes, 1 character.
    // MIN_TOKEN_NAME_LENGTH = 4 (documented as characters).
    // Passes because 4 >= 4 (bytes), but should fail (1 < 4 characters).
    let result = validate_token_name("🚀");
    assert!(result.is_ok(), "Single emoji name bypasses 4-char minimum: {:?}", result);
}
``` [7](#0-6)

### Citations

**File:** rs/nervous_system/common/src/ledger_validation.rs (L1-49)
```rust
/// The maximum number of characters allowed for token symbol.
pub const MAX_TOKEN_SYMBOL_LENGTH: usize = 10;

/// The minimum number of characters allowed for token symbol.
pub const MIN_TOKEN_SYMBOL_LENGTH: usize = 3;

/// The maximum number of characters allowed for token name.
pub const MAX_TOKEN_NAME_LENGTH: usize = 255;

/// The minimum number of characters allowed for token name.
pub const MIN_TOKEN_NAME_LENGTH: usize = 4;

/// The maximum number of characters allowed for a SNS logo encoding.
/// Roughly 256Kb
pub const MAX_LOGO_LENGTH: usize = 341334;

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

**File:** rs/sns/init/src/lib.rs (L961-977)
```rust
    fn validate_token_symbol(&self) -> Result<(), String> {
        let token_symbol = self
            .token_symbol
            .as_ref()
            .ok_or_else(|| "Error: token-symbol must be specified".to_string())?;

        ledger_validation::validate_token_symbol(token_symbol)
    }

    fn validate_token_name(&self) -> Result<(), String> {
        let token_name = self
            .token_name
            .as_ref()
            .ok_or_else(|| "Error: token-name must be specified".to_string())?;

        ledger_validation::validate_token_name(token_name)
    }
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

**File:** rs/sns/governance/src/lib.rs (L124-147)
```rust
/// Validate field value within some (message) struct.
fn validate_chars_count(
    field_name: &str,
    field_value: &str,
    min: usize,
    max: usize,
) -> Result<(), String> {
    let len = field_value.chars().count();

    if len < min {
        let defect = &format!("too short (min = {min} vs. observed = {len})");

        return field_err(field_name, field_value.to_string(), defect);
    }

    if len > max {
        let defect = &format!("too long (max = {max} vs. observed = {len})");
        let bounded_field_value = field_value.chars().take(max).collect::<String>();

        return field_err(field_name, bounded_field_value, defect);
    }

    Ok(())
}
```
