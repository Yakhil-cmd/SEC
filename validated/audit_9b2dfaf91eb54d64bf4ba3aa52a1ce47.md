### Title
SNS Treasury Manager `deposit` Lacks Minimum Return Enforcement, Enabling Sandwich Attacks on SNS Treasury Funds - (File: rs/sns/governance/src/extensions.rs)

---

### Summary

The `execute_treasury_manager_deposit` function in the SNS governance canister approves and deposits SNS treasury tokens into a Treasury Manager extension (e.g., a DEX liquidity pool adaptor) without enforcing any minimum return amount. The `DepositRequest` type carries no `min_lp_tokens_out` field, and the governance code performs no post-deposit validation of received assets. An attacker who is a large LP in the connected DEX can sandwich the deposit, causing the SNS treasury to receive significantly fewer LP tokens than expected at the time the proposal was approved.

---

### Finding Description

`execute_treasury_manager_deposit` in `rs/sns/governance/src/extensions.rs` follows a two-step flow:

1. Call `approve_treasury_manager` to set ICRC-2 allowances for the treasury manager canister.
2. Call `deposit` on the extension canister and log the returned balances. [1](#0-0) 

The `DepositRequest` type defined in the Treasury Manager interface contains only `allowances` — there is no `min_lp_tokens_out`, `min_return`, or any slippage-protection field: [2](#0-1) 

After the `deposit` call returns, the governance code only logs the response and returns `Ok(())` — it never checks whether the received LP tokens or assets meet any minimum threshold: [3](#0-2) 

The `treasury_manager.did` itself explicitly acknowledges this gap as a **Known Security Risk**: [4](#0-3) 

The proposal rendering code in `rs/sns/governance/src/proposal.rs` also warns about this exact attack class: [5](#0-4) 

Despite both warnings, the protocol-level code in `execute_treasury_manager_deposit` enforces no minimum return check whatsoever. The `ValidatedDepositOperationArg` struct only validates the amounts being sent out (`treasury_allocation_sns_e8s`, `treasury_allocation_icp_e8s`), not the amounts received back: [6](#0-5) 

This is the direct analog of the BAMM bug: in BAMM, `minCollateralReturn` was the only guard and could be gamed; here, there is **no guard at all** at the governance protocol layer.

---

### Impact Explanation

An attacker who is a large LP in a DEX connected to a registered Treasury Manager can manipulate the DEX price immediately before the SNS governance deposit executes. The SNS treasury deposits tokens at the manipulated price and receives significantly fewer LP tokens than the proposal voters expected. This constitutes a direct, irreversible financial loss of ICP and SNS tokens from the SNS treasury. The `approve_treasury_manager` step grants the extension canister an ICRC-2 allowance, and once `deposit` is called there is no rollback path if the return is unfavorable.

---

### Likelihood Explanation

SNS governance proposals are fully public and their execution timing is observable on-chain. A DEX canister controller or a large LP who monitors the IC can time a price manipulation to coincide with the deposit execution. On the IC there is no traditional mempool, but an attacker who controls or heavily influences a DEX canister can submit a price-moving transaction in the same or immediately preceding consensus round. The attacker does not need any privileged role — only a sufficiently large LP position in the connected DEX.

---

### Recommendation

1. Add a `min_lp_tokens_out` (or equivalent minimum return) field to `DepositRequest` in `rs/sns/treasury_manager/treasury_manager.did`.
2. Require SNS governance proposals that invoke `deposit` to specify this minimum return value, validated at proposal submission time.
3. In `execute_treasury_manager_deposit`, after the `deposit` call returns, decode and verify that the received LP token balance meets the minimum specified in the proposal. If not, treat the deposit as failed and attempt to recover the approved allowance.
4. Separate the slippage-protection check (minimum return) from any liquidity-availability check so that each concern is handled independently, preventing the dual-purpose parameter problem described in the reference report.

---

### Proof of Concept

1. An SNS governance proposal is submitted and approved to deposit `X` SNS tokens and `Y` ICP into a DEX via a registered Treasury Manager extension.
2. The attacker, a large LP in the target DEX, observes the proposal reaching execution state.
3. The attacker submits a large swap on the DEX canister that moves the SNS/ICP price ratio significantly against the SNS treasury's deposit direction.
4. `execute_treasury_manager_deposit` executes: `approve_treasury_manager` grants the ICRC-2 allowance, then `deposit` is called on the Treasury Manager.
5. The Treasury Manager deposits into the DEX at the manipulated price; the SNS treasury receives far fewer LP tokens than the proposal voters anticipated.
6. The attacker reverses the price manipulation (back-runs), extracting the value difference as profit.
7. The governance code logs the response and returns `Ok(())` — no minimum return check fires, no error is raised, and the loss is permanent.

### Citations

**File:** rs/sns/governance/src/extensions.rs (L1566-1609)
```rust
    // 1. Transfer funds from treasury to treasury manager
    governance
        .approve_treasury_manager(
            extension_canister_id,
            treasury_allocation_sns_e8s,
            treasury_allocation_icp_e8s,
        )
        .await?;

    // 2. Call deposit on treasury manager
    let balances = governance
        .env
        .call_canister(extension_canister_id, "deposit", arg_blob)
        .await
        .map_err(|(code, err)| {
            GovernanceError::new_with_message(
                ErrorType::External,
                format!(
                    "Canister method call {extension_canister_id}.deposit failed with code {code:?}: {err}"
                ),
            )
        })
        .and_then(|blob| {
            Decode!(&blob, sns_treasury_manager::TreasuryManagerResult).map_err(|err| {
                GovernanceError::new_with_message(
                    ErrorType::External,
                    format!("Error decoding TreasuryManager.deposit response: {err:?}"),
                )
            })
        })?
        .map_err(|err| {
            GovernanceError::new_with_message(
                ErrorType::External,
                format!("TreasuryManager.deposit failed: {err:?}"),
            )
        })?;

    log!(
        INFO,
        "TreasuryManager.deposit succeeded with response: {:?}",
        balances
    );

    Ok(())
```

**File:** rs/sns/governance/src/extensions.rs (L1663-1708)
```rust
/// Validated deposit operation arguments
#[derive(Debug, Clone)]
pub struct ValidatedDepositOperationArg {
    /// Amount of SNS tokens to allocate from treasury
    pub treasury_allocation_sns_e8s: u64,
    /// Amount of ICP tokens to allocate from treasury
    pub treasury_allocation_icp_e8s: u64,
    /// Original Precise value with all fields
    pub original: Precise,
}

impl TryFrom<Option<Precise>> for ValidatedDepositOperationArg {
    type Error = String;

    fn try_from(value: Option<Precise>) -> Result<Self, Self::Error> {
        let Some(original) = value else {
            return Err("Deposit operation arguments must be provided".to_string());
        };

        let map = match &original.value {
            Some(precise::Value::Map(PreciseMap { map })) => map,
            _ => return Err("Deposit operation arguments must be a PreciseMap".to_string()),
        };

        let treasury_allocation_sns_e8s = map
            .get("treasury_allocation_sns_e8s")
            .and_then(|p| match &p.value {
                Some(precise::Value::Nat(n)) => Some(*n),
                _ => None,
            })
            .ok_or_else(|| "treasury_allocation_sns_e8s must be a Nat value".to_string())?;

        let treasury_allocation_icp_e8s = map
            .get("treasury_allocation_icp_e8s")
            .and_then(|p| match &p.value {
                Some(precise::Value::Nat(n)) => Some(*n),
                _ => None,
            })
            .ok_or_else(|| "treasury_allocation_icp_e8s must be a Nat value".to_string())?;

        Ok(Self {
            treasury_allocation_sns_e8s,
            treasury_allocation_icp_e8s,
            original,
        })
    }
```

**File:** rs/sns/treasury_manager/treasury_manager.did (L35-40)
```text
// Known Security Risks:
// ====================
// Some liquidity pools do not implement slippage protection
// for deposits. In other words, the price ratio at the time
// of execution may differ from the ratio at the time the proposal was
// approved.
```

**File:** rs/sns/treasury_manager/treasury_manager.did (L84-86)
```text
type DepositRequest = record {
  allowances : vec Allowance;
};
```

**File:** rs/sns/governance/src/proposal.rs (L1540-1545)
```rust
## WARNING

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.
```
