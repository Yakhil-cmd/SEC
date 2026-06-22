### Title
Fixed-Ratio SNS Treasury Deposit Ignores Pool Imbalance at Execution Time - (`rs/sns/governance/src/extensions.rs`)

### Summary
The SNS governance `execute_treasury_manager_deposit` function deposits treasury assets (SNS tokens + ICP) into DEX liquidity pools using token amounts that are fixed at proposal creation time. Because governance proposals have multi-day voting periods, the pool ratio can shift significantly before execution. When the deposit executes with the original fixed ratio against an imbalanced pool, the SNS treasury receives fewer LP tokens than optimal — a permanent loss of treasury value for SNS token holders. The codebase itself acknowledges this as a "Known Security Risk" but provides no on-chain mitigation.

### Finding Description

The deposit flow in `rs/sns/governance/src/extensions.rs` is:

1. A governance proposal is submitted with fixed `treasury_allocation_sns_e8s` and `treasury_allocation_icp_e8s` values.
2. At proposal creation, `validate_deposit_operation_impl` checks only that each requested amount does not exceed 50% of the current treasury balance.
3. After the voting period, `execute_treasury_manager_deposit` is called. It uses the same fixed amounts from the proposal, calls `approve_treasury_manager` to grant ICRC-2 allowances, then calls `deposit` on the treasury manager canister. [1](#0-0) 

The validation function `validate_deposit_operation_impl` performs no check of the current DEX pool ratio: [2](#0-1) 

The `ValidatedDepositOperationArg` struct simply stores the fixed amounts from the proposal: [3](#0-2) 

The codebase explicitly acknowledges this risk in the treasury manager interface definition: [4](#0-3) 

And in the proposal rendering warning shown to voters: [5](#0-4) 

Despite these acknowledgements, no slippage protection or pool-ratio check is enforced at execution time in the governance canister itself. The `execute_treasury_manager_deposit` function blindly forwards the proposal-time amounts to the treasury manager.

### Impact Explanation

When the SNS/ICP pool ratio at execution time differs from the ratio encoded in the proposal, the deposit is imbalanced relative to the pool. DEX pools penalize imbalanced deposits (or internally swap the excess token, incurring fees), resulting in fewer LP tokens minted to the SNS treasury. This is a permanent, irreversible loss of treasury value for all SNS token holders. The loss scales with the degree of pool imbalance and the deposit size (up to 50% of treasury per proposal).

### Likelihood Explanation

SNS governance proposals have voting periods measured in days. During that window, pool ratios change continuously due to normal trading activity. Additionally, a malicious actor who observes a pending deposit proposal can deliberately sandwich the execution: manipulate the pool ratio just before the proposal executes to maximize the slippage penalty, then arbitrage the pool back to profit at the SNS treasury's expense. No privileged access is required — any user can submit transactions to the DEX.

### Recommendation

1. Add a `min_lp_tokens_out` parameter to the deposit proposal that the treasury manager must enforce at execution time, providing explicit slippage protection.
2. Alternatively, re-validate the proposed SNS:ICP ratio against the current pool ratio at execution time and abort if the deviation exceeds a governance-configured threshold.
3. Consider time-locking the deposit execution to a short window after proposal passage to reduce the attack surface.

### Proof of Concept

1. **T0**: The SNS/ICP pool is balanced at 50%/50% (e.g., 1,000,000 SNS and 1,000,000 ICP).
2. **T1**: An SNS governance proposal is submitted to deposit 10,000 SNS and 10,000 ICP (1:1 ratio, matching the pool). `validate_deposit_operation_impl` passes because each amount is below 50% of treasury balance.
3. **T2** (during voting period): Market activity shifts the pool to 30% SNS / 70% ICP (e.g., 600,000 SNS and 1,400,000 ICP).
4. **T3**: The proposal passes and `execute_treasury_manager_deposit` executes with the original 10,000 SNS / 10,000 ICP amounts. The pool now expects a 30:70 ratio; the 1:1 deposit is imbalanced.
5. **Result**: The DEX penalizes the imbalanced deposit. The SNS treasury receives fewer LP tokens than it would have if the deposit ratio had been adjusted to match the current 30:70 pool state. The shortfall is permanent — the SNS treasury cannot recover the lost LP value.

A malicious actor at step 3 can amplify this by swapping a large amount of SNS into the pool just before step 4 executes (worsening the imbalance), then swapping back after the deposit to profit from the arbitrage, at the SNS treasury's expense.

### Citations

**File:** rs/sns/governance/src/extensions.rs (L276-321)
```rust
async fn validate_deposit_operation_impl(
    governance: &Governance,
    value: Option<Precise>,
) -> Result<ValidatedDepositOperationArg, String> {
    let structurally_valid = ValidatedDepositOperationArg::try_from(value)?;

    let sns_subaccount = governance.sns_treasury_subaccount();
    let icp_subaccount = governance.icp_treasury_subaccount();

    // Fail if either is asking for more than 50% of current balance.  The balance could have changed
    // since the proposal was created, and we don't assume that the proposal should work
    let sns_balance = governance
        .ledger
        .account_balance(Account {
            owner: governance.env.canister_id().get().0,
            subaccount: sns_subaccount,
        })
        .await
        .map_err(|e| format!("Failed to get SNS treasury balance: {e:?}"))?;
    let icp_balance = governance
        .nns_ledger
        .account_balance(Account {
            owner: governance.env.canister_id().get().0,
            subaccount: icp_subaccount,
        })
        .await
        .map_err(|e| format!("Failed to get ICP treasury balance: {e:?}"))?;

    let icp_requested = Tokens::from_e8s(structurally_valid.treasury_allocation_icp_e8s);
    let sns_requested = Tokens::from_e8s(structurally_valid.treasury_allocation_sns_e8s);

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

    Ok(structurally_valid)
}
```

**File:** rs/sns/governance/src/extensions.rs (L1545-1573)
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

**File:** rs/sns/governance/src/proposal.rs (L1540-1545)
```rust
## WARNING

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.
```
