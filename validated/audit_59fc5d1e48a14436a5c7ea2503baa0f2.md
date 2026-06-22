### Title
SNS Treasury Manager Deposit Proposal Lacks On-Chain Slippage Protection, Enabling Front-Running and Fund Loss - (File: rs/sns/governance/src/extensions.rs)

### Summary
The SNS governance deposit operation for treasury manager extensions validates caller-supplied token amounts only against a 50% treasury balance cap, but never enforces any ratio constraint or minimum-LP-out (slippage protection). A governance proposal encodes `treasury_allocation_sns_e8s` and `treasury_allocation_icp_e8s` at proposal-creation time, but execution happens after the voting period (potentially days later). An attacker can front-run the on-chain execution to manipulate the DEX pool ratio, causing the SNS treasury to deposit at a mispriced ratio and lose funds to the attacker. The codebase itself acknowledges this gap as a "Known Security Risk" but provides no on-chain enforcement.

### Finding Description

`validate_deposit_operation_impl` (called from `validate_deposit_operation`) validates the two caller-supplied amounts only against the 50% treasury balance limit: [1](#0-0) 

The `ValidatedDepositOperationArg` struct and its `TryFrom` implementation parse `treasury_allocation_sns_e8s` and `treasury_allocation_icp_e8s` from the proposal payload but impose no constraint on their ratio relative to the current DEX pool state: [2](#0-1) 

`execute_treasury_manager_deposit` then approves these exact amounts and calls `deposit` on the treasury manager canister with no minimum-LP-out guard: [3](#0-2) 

The same gap exists during the initial extension registration path (`ValidatedRegisterExtension::execute`), which is the **first-deposit** scenario directly analogous to the Numoen finding: [4](#0-3) 

The codebase itself acknowledges the risk in the treasury manager interface definition: [5](#0-4) 

And the proposal rendering function emits a human-readable warning but enforces nothing on-chain: [6](#0-5) 

The two caller-supplied values (`treasury_allocation_sns_e8s`, `treasury_allocation_icp_e8s`) must satisfy the DEX pool's current token ratio at execution time. No on-chain helper computes the correct ratio, and no minimum-LP-out parameter is accepted or enforced. This is structurally identical to the Numoen M-02 pattern: multiple caller-supplied values must satisfy a mathematical relationship that is not validated on-chain, and the gap between proposal creation and execution creates a window for exploitation.

### Impact Explanation

**Fund loss to the SNS treasury.** Two failure modes exist:

1. **Revert / deposit rejection**: If the DEX enforces strict ratio bounds, the deposit call fails. The approved allowance may be partially consumed by ledger fees, and the operation leaves the treasury manager in an inconsistent state requiring manual recovery.

2. **Silent mispricing / value extraction**: If the DEX accepts deposits at any ratio (common in constant-product AMMs), the SNS treasury deposits at the attacker-manipulated ratio. The attacker then arbitrages the mispriced pool, extracting value that was donated by the SNS treasury. For the first-deposit case (extension registration), the attacker can set the pool's initial price arbitrarily far from fair value and immediately drain the mispriced side.

The `treasury_manager.did` note that "any undeposited tokens are automatically returned" only applies to tokens the DEX refuses; tokens accepted at a bad ratio are permanently lost.

### Likelihood Explanation

SNS governance proposals have voting periods measured in days. During this window:

- Any observer can monitor the pending proposal and its encoded `treasury_allocation_sns_e8s` / `treasury_allocation_icp_e8s` values.
- A front-runner can trade on the target DEX pool to shift its ratio before the proposal executes, then arbitrage back after execution.
- No privileged access is required; the attacker only needs to be a normal DEX user.
- The attack is profitable whenever the SNS treasury deposit is large relative to pool liquidity, which is the typical case for a DAO deploying treasury funds.

### Recommendation

1. Add a `min_lp_tokens_out` (or equivalent minimum-amount-out) field to the deposit proposal payload and enforce it on-chain at execution time inside `execute_treasury_manager_deposit`.
2. Add an on-chain preview/simulation call to the DEX before approving the allowance, and abort if the expected LP tokens fall below the proposal-specified minimum.
3. Consider adding a maximum staleness bound: if the proposal was adopted more than N seconds ago and the pool ratio has drifted beyond a threshold, reject execution and require a fresh proposal.
4. Expose a query function on the treasury manager canister that computes the correct ratio for a given liquidity target, so proposal authors can supply accurate values.

### Proof of Concept

1. An SNS governance proposal is submitted encoding `treasury_allocation_sns_e8s = 1_000_000` and `treasury_allocation_icp_e8s = 1_000_000` (1:1 ratio, reflecting the pool state at proposal creation).
2. During the multi-day voting period, an attacker trades on the DEX pool, shifting its ratio to 3:1 (SNS:ICP).
3. The proposal passes and `execute_treasury_manager_deposit` is called. `validate_deposit_operation_impl` confirms both amounts are within the 50% treasury cap — the only check performed.
4. The SNS governance canister approves the treasury manager for `1_000_000` SNS tokens and `1_000_000` ICP tokens, then calls `deposit`.
5. The DEX accepts the deposit at the current 3:1 ratio. The SNS treasury effectively contributes 3× more SNS value than ICP value relative to the pool price, donating the excess to existing LPs or to the attacker who immediately arbitrages the imbalance.
6. The attacker profits; the SNS treasury loses the mispriced portion with no on-chain mechanism to detect or prevent it.

### Citations

**File:** rs/sns/governance/src/extensions.rs (L384-394)
```rust
fn validate_deposit_operation(
    governance: &Governance,
    arg: ExtensionOperationArg,
) -> BoxFuture<'_, Result<ValidatedOperationArg, String>> {
    Box::pin(async move {
        let ExtensionOperationArg { value } = arg;
        validate_deposit_operation_impl(governance, value)
            .await
            .map(ValidatedOperationArg::TreasuryManagerDeposit)
    })
}
```

**File:** rs/sns/governance/src/extensions.rs (L506-555)
```rust
    pub async fn execute(self, governance: &Governance) -> Result<(), GovernanceError> {
        let main = async || {
            let context = governance.treasury_manager_deposit_context().await?;

            let ValidatedRegisterExtension {
                spec,
                init,
                extension_canister_id,
                wasm,
            } = self;

            governance
                .register_extension_with_root(extension_canister_id)
                .await?;

            // Before granting any SNS capabilities to the extension, we must ensure that old code
            // could not have snuck in between proposal (re-)validation and the SNS assuming control.
            governance
                .ensure_no_code_is_installed(extension_canister_id)
                .await?;

            // This needs to happen before the canister code is installed.
            let init_blob = match init {
                ValidatedExtensionInit::TreasuryManager(ValidatedDepositOperationArg {
                    treasury_allocation_sns_e8s,
                    treasury_allocation_icp_e8s,
                    original,
                }) => {
                    let init_blob =
                        construct_treasury_manager_init_payload(context.clone(), original)
                            .map_err(|err| {
                                GovernanceError::new_with_message(
                                    ErrorType::InvalidProposal,
                                    format!(
                                        "Error constructing TreasuryManagerInit payload: {err}"
                                    ),
                                )
                            })?;

                    governance
                        .approve_treasury_manager(
                            extension_canister_id,
                            treasury_allocation_sns_e8s,
                            treasury_allocation_icp_e8s,
                        )
                        .await?;

                    init_blob
                }
            };
```

**File:** rs/sns/governance/src/extensions.rs (L1545-1609)
```rust
/// Execute a treasury manager deposit operation
async fn execute_treasury_manager_deposit(
    governance: &Governance,
    extension_canister_id: CanisterId,
    arg: ValidatedDepositOperationArg,
) -> Result<(), GovernanceError> {
    let ValidatedDepositOperationArg {
        treasury_allocation_sns_e8s,
        treasury_allocation_icp_e8s,
        original,
    } = arg;

    let context = governance.treasury_manager_deposit_context().await?;
    let arg_blob =
        construct_treasury_manager_deposit_payload(context, original).map_err(|err| {
            GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!("Failed to construct treasury manager deposit payload: {err}"),
            )
        })?;

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

**File:** rs/sns/governance/src/proposal.rs (L1540-1550)
```rust
## WARNING

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.

## Extension Configuration

The extension will be deployed and configured according to the provided parameters.",
    ))
```
