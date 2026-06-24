### Title
Lack of Slippage Control in SNS Treasury Manager `deposit` API Exposes SNS Treasury to Front-Running Losses - (File: `rs/sns/treasury_manager/treasury_manager.did`, `rs/sns/governance/src/extensions.rs`)

---

### Summary

The `DepositRequest` type in the SNS Treasury Manager API and the `execute_treasury_manager_deposit` function in SNS Governance lack any slippage protection parameters. When an SNS governance proposal deposits treasury funds into a DEX via a TreasuryManager extension, there is no mechanism to specify a minimum amount of LP tokens to receive in return, exposing the SNS treasury to front-running and sandwich attacks. The codebase itself acknowledges this as a known risk but does not enforce any mitigation at the protocol level.

---

### Finding Description

The `DepositRequest` record in `treasury_manager.did` only contains `allowances` (the amounts to deposit) but no `min_lp_tokens` or equivalent field:

```candid
type DepositRequest = record {
  allowances : vec Allowance;
};
``` [1](#0-0) 

The codebase explicitly acknowledges the risk in the same file:

```
// Known Security Risks:
// ====================
// Some liquidity pools do not implement slippage protection
// for deposits. In other words, the price ratio at the time
// of execution may differ from the ratio at the time the proposal was
// approved.
``` [2](#0-1) 

The `execute_treasury_manager_deposit` function in `extensions.rs` approves treasury funds and calls `deposit` on the TreasuryManager canister, but does not validate the returned `Balances` against any minimum expected LP token amount:

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
``` [3](#0-2) 

The `ValidatedDepositOperationArg` struct only validates `treasury_allocation_sns_e8s` and `treasury_allocation_icp_e8s` — no minimum return is specified or checked: [4](#0-3) 

The proposal rendering code in `proposal.rs` also warns about this but provides no enforcement:

```
## WARNING
Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.
``` [5](#0-4) 

The note that "undeposited tokens are automatically returned" does not protect against the core slippage scenario: tokens that *are* deposited at a manipulated price ratio result in fewer LP tokens than expected, and those LP tokens are not returned.

---

### Impact Explanation

SNS treasury funds (SNS tokens + ICP) can be deposited into a DEX at an unfavorable price ratio. A malicious actor can execute a sandwich attack: buy SNS tokens before the deposit to move the price, allow the SNS governance to deposit at the worse price, then sell after. The SNS receives fewer LP tokens than expected for the deposited assets, resulting in a direct, irreversible financial loss to the SNS treasury. Since SNS governance proposals are public and their execution is deterministic and observable on-chain, the attack window is well-defined.

---

### Likelihood Explanation

SNS governance proposals are publicly visible on-chain. Any actor monitoring the IC can observe a pending `ExecuteExtensionOperation` deposit proposal and front-run its execution. The time between proposal approval and execution (which can span hours or days) provides ample opportunity. No special privileges are required — any boundary/API user or canister caller can submit the front-running transaction.

---

### Recommendation

1. Add a `min_lp_tokens` or `min_shares` field to `DepositRequest` in `treasury_manager.did` so that the SNS governance proposal can specify the minimum acceptable LP token return.
2. In `execute_treasury_manager_deposit` (`extensions.rs`), validate the returned `Balances` against the minimum expected LP tokens and revert (or log a governance failure) if the minimum is not met.
3. Add `min_lp_tokens_e8s` to `ValidatedDepositOperationArg` and enforce it during proposal validation.

---

### Proof of Concept

1. An SNS governance proposal is submitted to deposit 1,000 SNS tokens + 1,000 ICP into a DEX via `ExecuteExtensionOperation` with `operation_name = "deposit"`.
2. The proposal passes and is queued for execution.
3. An attacker observes the pending execution on-chain and front-runs: buys SNS tokens to move the pool price unfavorably.
4. SNS governance executes `execute_treasury_manager_deposit`, which calls `deposit` on the TreasuryManager canister with no minimum LP token constraint.
5. The TreasuryManager deposits into the DEX at the manipulated price, receiving significantly fewer LP tokens than the pre-proposal price would have yielded.
6. The attacker sells their SNS tokens at the inflated price, profiting from the spread.
7. The SNS treasury has lost value: it deposited 1,000 SNS + 1,000 ICP but received LP tokens worth materially less, with no recourse.

### Citations

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

**File:** rs/sns/governance/src/extensions.rs (L1566-1601)
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

**File:** rs/sns/governance/src/proposal.rs (L1540-1545)
```rust
## WARNING

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.
```
