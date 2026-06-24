I have enough information to write the finding. The key asymmetry is in `rs/nervous_system/common/src/ledger_validation.rs`: `validate_token_name` strips all internal whitespace before checking against banned names, but `validate_token_symbol` does not — it only rejects leading/trailing whitespace. This allows bypassing the banned symbol list (`["ICP", "DFINITY"]`) by inserting internal spaces (e.g., `"I C P"`).

---

### Title
Banned Token Symbol Check Bypassed via Internal Whitespace — (`rs/nervous_system/common/src/ledger_validation.rs`)

### Summary
`validate_token_symbol` does not strip internal whitespace before comparing against `BANNED_TOKEN_SYMBOLS`, while `validate_token_name` explicitly strips all whitespace before comparing against `BANNED_TOKEN_NAMES`. An SNS creator or SNS governance participant can submit a `CreateServiceNervousSystem` or `ManageLedgerParameters` proposal with a token symbol such as `"I C P"` or `"D F I N I T Y"`, which passes validation and is accepted on-chain, bypassing the intended brand-protection restriction.

### Finding Description
`BANNED_TOKEN_SYMBOLS` is defined as `["ICP", "DFINITY"]` and `BANNED_TOKEN_NAMES` as `["internetcomputer", "internetcomputerprotocol"]`. [1](#0-0) 

`validate_token_name` strips all Unicode whitespace characters (including internal ones) before the banned-name lookup: [2](#0-1) 

`validate_token_symbol`, however, only rejects leading/trailing whitespace via a `trim()` equality check, and then compares the raw (uppercased but whitespace-preserving) string against the banned list: [3](#0-2) 

Because `"I C P".to_uppercase()` is `"I C P"`, which is not equal to `"ICP"`, the banned-symbol check passes. The same applies to `"D F I N I T Y"`.

Both the SNS-creation path (`SnsInitPayload::validate_token_symbol`) and the post-launch update path (`validate_and_render_manage_ledger_parameters`) call the same `validate_token_symbol` function and are equally affected: [4](#0-3) [5](#0-4) 

### Impact Explanation
An SNS token with symbol `"I C P"` or `"D F I N I T Y"` can be deployed on mainnet and will appear in wallets, explorers, and DEX UIs with a symbol visually indistinguishable from the real ICP or DFINITY brand identifiers. This undermines the brand-protection guarantee that `BANNED_TOKEN_SYMBOLS` is meant to enforce, enabling impersonation and phishing of ICP holders.

### Likelihood Explanation
The attacker-controlled entry path is an unprivileged ingress call: any principal can submit a `CreateServiceNervousSystem` NNS proposal (requiring only a neuron with sufficient stake) or, once an SNS exists, a `ManageLedgerParameters` SNS governance proposal. No privileged role, leaked key, or majority corruption is required. The bypass requires only knowledge of the whitespace gap in the validation logic.

### Recommendation
Strip all internal whitespace from the token symbol before comparing against `BANNED_TOKEN_SYMBOLS`, mirroring the treatment already applied in `validate_token_name`:

```rust
if BANNED_TOKEN_SYMBOLS.contains(
    &token_symbol
        .to_uppercase()
        .chars()
        .filter(|c| !c.is_whitespace())
        .collect::<String>()
        .as_ref(),
) {
    return Err("Banned token symbol, please chose another one.".to_string());
}
``` [6](#0-5) 

### Proof of Concept
1. Construct a `CreateServiceNervousSystem` NNS proposal with `ledger_parameters.token_symbol = "I C P"`.
2. Submit it via NNS governance. `validate_create_service_nervous_system` calls `SnsInitPayload::try_from(...)` which calls `validate_token_symbol("I C P")`.
3. `"I C P" == "I C P".trim()` → trim check passes. `["ICP","DFINITY"].contains(&"I C P")` → banned check passes.
4. The proposal is accepted and the SNS is deployed with token symbol `"I C P"`.
5. Alternatively, after SNS launch, submit a `ManageLedgerParameters` proposal with `token_symbol = Some("I C P".to_string())`. `validate_and_render_manage_ledger_parameters` calls `validate_token_symbol("I C P")`, which passes for the same reason, and the ledger is upgraded with the impersonating symbol. [7](#0-6) [8](#0-7)

### Citations

**File:** rs/nervous_system/common/src/ledger_validation.rs (L17-21)
```rust
/// Token Symbols that can not be used.
const BANNED_TOKEN_SYMBOLS: &[&str] = &["ICP", "DFINITY"];

/// Token Names that can not be used.
const BANNED_TOKEN_NAMES: &[&str] = &["internetcomputer", "internetcomputerprotocol"];
```

**File:** rs/nervous_system/common/src/ledger_validation.rs (L40-46)
```rust
    if token_symbol != token_symbol.trim() {
        return Err("Token symbol must not have leading or trailing whitespaces".to_string());
    }

    if BANNED_TOKEN_SYMBOLS.contains(&token_symbol.to_uppercase().as_ref()) {
        return Err("Banned token symbol, please chose another one.".to_string());
    }
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
