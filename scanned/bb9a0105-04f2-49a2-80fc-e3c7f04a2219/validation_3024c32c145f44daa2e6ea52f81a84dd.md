### Title
SNS Treasury Manager Deposit Lacks On-Chain Slippage Protection, Enabling Front-Running / Sandwich Attacks Against the SNS Treasury - (File: `rs/sns/governance/src/extensions.rs`)

---

### Summary

The SNS governance canister's `execute_treasury_manager_deposit` function approves and deposits SNS and ICP treasury funds into an external DEX (KongSwap adaptor) without enforcing any minimum price ratio or slippage bound at execution time. Because SNS governance proposals have a multi-day voting period, an attacker can observe an adopted deposit proposal and manipulate the DEX pool price before the proposal executes, causing the SNS treasury to deposit assets at a severely unfavorable ratio and extracting value from the DAO.

---

### Finding Description

The SNS extension framework allows an SNS DAO to deposit treasury assets (SNS tokens + ICP) into a DEX liquidity pool via a `TreasuryManager` extension canister (e.g., the KongSwap adaptor). The flow is:

1. An SNS neuron holder submits an `ExecuteExtensionOperation` proposal specifying `treasury_allocation_sns_e8s` and `treasury_allocation_icp_e8s`.
2. The proposal undergoes a voting period (days).
3. On adoption, `execute_treasury_manager_deposit` is called.

Inside `execute_treasury_manager_deposit`:

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
    ...
``` [1](#0-0) 

The validation step (`validate_deposit_operation_impl`) only checks that the requested amounts do not exceed 50% of the current treasury balance:

```rust
if sns_requested > sns_balance.checked_div(2).unwrap() { ... }
if icp_requested > icp_balance.checked_div(2).unwrap() { ... }
``` [2](#0-1) 

There is **no check** on the current pool price ratio, no minimum LP tokens out, and no slippage bound. The `DepositRequest` passed to the extension canister carries only the raw token allowances, not any price constraint: [3](#0-2) 

The codebase itself acknowledges this risk in two places but does not implement a protocol-level fix:

- In `treasury_manager.did`: *"Some liquidity pools do not implement slippage protection for deposits. In other words, the price ratio at the time of execution may differ from the ratio at the time the proposal was approved."* [4](#0-3) 

- In `proposal.rs` rendering for `RegisterExtension`: *"This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running or sandwich attacks."* [5](#0-4) 

---

### Impact Explanation

An attacker who monitors the SNS governance canister for adopted (but not yet executed) deposit proposals can:

1. Observe the exact SNS/ICP amounts to be deposited.
2. Manipulate the KongSwap pool price (e.g., by swapping a large amount to skew the ratio) before the proposal executes.
3. The SNS governance executes the deposit at the manipulated price, receiving far fewer LP tokens than the fair-market value of the deposited assets.
4. The attacker reverses their manipulation trade, profiting from the spread.

The SNS treasury permanently loses value proportional to the price impact of the manipulation. Because the voting period is days long, the attacker has ample time to prepare capital and execute the sandwich. The 50% balance cap limits the maximum single-deposit loss but does not prevent the attack.

**Vulnerability class**: Ledger conservation bug / governance authorization bug — SNS treasury assets are transferred at an attacker-controlled price with no on-chain bound.

---

### Likelihood Explanation

- The attack requires no privileged access: any unprivileged canister caller or boundary/API user with sufficient capital can execute it.
- SNS governance proposals are public and their adoption is observable on-chain.
- The multi-day voting period gives the attacker days of preparation time.
- DEX pool manipulation is a well-known, routinely executed attack class on-chain.
- The KongSwap adaptor is the only blessed extension and is referenced in production integration tests. [6](#0-5) 

Likelihood: **Medium** — requires capital for pool manipulation, but the attack window is large and the target (SNS treasury) is publicly known.

---

### Recommendation

1. **Add a `min_lp_tokens_out` or `min_price_ratio` field to `DepositRequest`** so the extension canister can enforce a slippage bound atomically at deposit time.
2. **Encode the expected price ratio at proposal creation time** in the `ExecuteExtensionOperation` arguments and validate it at execution time against a fresh on-chain price query.
3. **Implement a maximum staleness window**: if the pool price has moved more than X% since proposal creation, abort the deposit.
4. **Consider a time-lock or commit-reveal** between proposal adoption and execution to reduce the predictability of the execution block.

---

### Proof of Concept

**Setup**: An SNS DAO has 200 SNS tokens and 100 ICP in its treasury. A deposit proposal is submitted to deposit 100 SNS + 50 ICP into the KongSwap SNS/ICP pool at the current fair ratio of 2:1.

**Attack**:

1. Attacker observes the proposal is adopted (voting period ends). Execution will happen at the next heartbeat.
2. Attacker calls KongSwap directly, swapping a large amount of ICP for SNS tokens, moving the pool price to 4:1 (SNS is now "cheaper" relative to ICP in the pool).
3. SNS governance heartbeat fires, `execute_treasury_manager_deposit` runs:
   - `approve_treasury_manager` grants the adaptor allowance for 100 SNS + 50 ICP.
   - `deposit` is called with no price constraint; the adaptor deposits at the manipulated 4:1 ratio.
   - The SNS treasury receives LP tokens worth only ~75 ICP equivalent instead of the fair ~100 ICP equivalent.
4. Attacker reverses their swap, restoring the pool price and pocketing the ~25 ICP equivalent spread.

The root cause is in `execute_treasury_manager_deposit` at: [7](#0-6) 

combined with the absence of any slippage parameter in `ValidatedDepositOperationArg`: [8](#0-7)

### Citations

**File:** rs/sns/governance/src/extensions.rs (L307-318)
```rust
    // Unwrap is safe, only fails if divisor is zero, which we don't do.
    if sns_requested > sns_balance.checked_div(2).unwrap() {
        return Err(format!(
            "SNS treasury deposit request of {sns_requested} exceeds 50% of current SNS Token balance of {sns_balance}"
        ));
    }

    if icp_requested > icp_balance.checked_div(2).unwrap() {
        return Err(format!(
            "ICP treasury deposit request of {icp_requested} exceeds 50% of current ICP balance of {icp_balance}"
        ));
    }
```

**File:** rs/sns/governance/src/extensions.rs (L1545-1610)
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
}
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

**File:** rs/nervous_system/integration_tests/tests/sns_extension_test.rs (L229-231)
```rust
        let wasm_path = std::env::var("KONGSWAP_ADAPTOR_CANISTER_WASM_PATH")
            .expect("KONGSWAP_ADAPTOR_CANISTER_WASM_PATH must be set.");

```
