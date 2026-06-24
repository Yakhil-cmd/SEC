### Title
SNS Treasury Manager `DepositRequest` and `WithdrawRequest` Lack Slippage Protection, Exposing DAO Treasury to Front-Running - (File: `rs/sns/treasury_manager/treasury_manager.did`, `rs/sns/governance/src/extensions.rs`)

---

### Summary

The SNS Treasury Manager API defines `DepositRequest` and `WithdrawRequest` types that contain no minimum-output or deadline parameters. When an SNS governance proposal to deposit or withdraw treasury funds into a DEX liquidity pool is executed, the amounts received are determined entirely by the DEX's pool state at execution time. Because governance proposals require a multi-day voting period, the window between proposal creation and execution is far larger than in a typical user transaction, making the SNS treasury uniquely exposed to front-running and sandwich attacks.

---

### Finding Description

The `DepositRequest` type in the Treasury Manager API only carries `allowances` (the amounts to send), and `WithdrawRequest` only carries optional `withdraw_accounts`. Neither type includes a `min_lp_tokens_out`, `min_asset_amounts_out`, or `deadline` field. [1](#0-0) 

The codebase itself acknowledges this gap in two places. The `.did` file notes it as a "Known Security Risk": [2](#0-1) 

And the `validate_and_render_register_extension` function in SNS Governance emits a warning in the proposal rendering that DEXes may lack slippage protection: [3](#0-2) 

The `ValidatedDepositOperationArg` struct parsed from the governance proposal only extracts `treasury_allocation_sns_e8s` and `treasury_allocation_icp_e8s`. No minimum LP token output or price bound is parsed or enforced: [4](#0-3) 

The `ValidatedWithdrawOperationArg` is even more constrained — it explicitly rejects any arguments at all, making it impossible to pass slippage bounds even if a caller wanted to: [5](#0-4) 

The execution path for deposit calls `deposit` on the treasury manager canister and logs the result, but performs no check on the returned balances to verify that the SNS treasury received an acceptable amount of LP tokens: [6](#0-5) 

Similarly, `execute_treasury_manager_withdraw` calls `withdraw` and logs the result without verifying minimum asset amounts returned to the treasury: [7](#0-6) 

The `validate_deposit_operation_impl` function does check that the requested deposit does not exceed 50% of the current treasury balance, but this is a cap on the *input*, not a floor on the *output*: [8](#0-7) 

---

### Impact Explanation

An SNS DAO's treasury can suffer a direct, quantifiable loss of value. When depositing liquidity, the DAO may receive significantly fewer LP tokens than the pool ratio implied at proposal creation time. When withdrawing, the DAO may receive an unfavorable mix of assets, crystallizing impermanent loss at the worst possible moment. Because the SNS treasury holds community-owned funds (SNS tokens and ICP), this loss is borne by all token holders. The `TreasuryAssetManagement` topic is marked `is_critical: true`, meaning these proposals already require heightened scrutiny, yet the protocol itself provides no on-chain enforcement of acceptable outcomes. [9](#0-8) 

---

### Likelihood Explanation

The likelihood is **high** for any SNS that actively uses the Treasury Manager extension. The governance voting period for critical proposals is measured in days. Any DEX participant (an unprivileged canister caller) can execute trades during this window to shift pool reserves. The attacker does not need any privileged access — only the ability to trade on the same DEX pool. The codebase's own comments confirm awareness of this exact risk, indicating it is a known, unmitigated condition in the current production API.

---

### Recommendation

1. **Add slippage parameters to `DepositRequest` and `WithdrawRequest`** in `rs/sns/treasury_manager/treasury_manager.did`: include optional `min_lp_tokens_out` for deposits and `min_asset_amounts_out : vec record { principal; nat }` for withdrawals.
2. **Parse and enforce these bounds** in `ValidatedDepositOperationArg` and `ValidatedWithdrawOperationArg` in `rs/sns/governance/src/extensions.rs`, and verify the returned `Balances` from the treasury manager canister against them in `execute_treasury_manager_deposit` and `execute_treasury_manager_withdraw`.
3. **Add an optional `deadline_ns` field** to both request types so that proposals that sit in the queue for an unexpectedly long time can be rejected rather than executed at stale prices.

---

### Proof of Concept

1. An SNS submits an `ExecuteExtensionOperation` governance proposal to deposit 100,000 SNS tokens and 50,000 ICP into a KongSwap liquidity pool at the current ratio of 2:1.
2. The proposal enters the voting period (e.g., 4 days for a critical proposal).
3. During the voting period, an attacker executes a large buy of SNS tokens from the pool, shifting the ratio to 4:1 (SNS is now twice as expensive in ICP terms).
4. The proposal passes and `execute_treasury_manager_deposit` is called. It calls `governance.approve_treasury_manager(...)` with the original amounts and then calls `deposit` on the treasury manager canister.
5. The treasury manager deposits into the pool at the new 4:1 ratio. Because the SNS deposit is now over-valued relative to ICP, only a fraction of the ICP can be paired; the remainder is returned, but the LP tokens minted reflect the distorted ratio.
6. The SNS treasury receives fewer LP tokens than it would have at the original ratio, and the attacker can reverse their trade (sandwich) to profit from the price impact, with the DAO bearing the loss.
7. No check in `execute_treasury_manager_deposit` or anywhere in the governance execution path rejects or reverts this outcome. [10](#0-9) [1](#0-0)

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

**File:** rs/sns/treasury_manager/treasury_manager.did (L84-93)
```text
type DepositRequest = record {
  allowances : vec Allowance;
};

type WithdrawRequest = record {
  // Maps Ledger canister IDs of assets to be withdrawn to the respective withdraw accounts.
  //
  // If not set, accounts specified at the time of deposit will be used for the withdrawal.
  withdraw_accounts : opt vec record { principal; Account };
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

**File:** rs/sns/governance/src/extensions.rs (L276-320)
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

**File:** rs/sns/governance/src/extensions.rs (L1612-1660)
```rust
/// Execute a treasury manager withdraw operation
async fn execute_treasury_manager_withdraw(
    governance: &Governance,
    extension_canister_id: CanisterId,
    arg: ValidatedWithdrawOperationArg,
) -> Result<(), GovernanceError> {
    let arg_blob = construct_treasury_manager_withdraw_payload(arg.original).map_err(|err| {
        GovernanceError::new_with_message(
            ErrorType::PreconditionFailed,
            format!("Failed to construct treasury manager withdraw payload: {err}"),
        )
    })?;

    let balances = governance
        .env
        .call_canister(extension_canister_id, "withdraw", arg_blob)
        .await
        .map_err(|(code, err)| {
            GovernanceError::new_with_message(
                ErrorType::External,
                format!(
                    "Canister method call {extension_canister_id}.withdraw failed with code {code:?}: {err}"
                ),
            )
        })
        .and_then(|blob| {
            Decode!(&blob, sns_treasury_manager::TreasuryManagerResult).map_err(|err| {
                GovernanceError::new_with_message(
                    ErrorType::External,
                    format!(
                        "Error decoding TreasuryManager.withdraw response: {err:?}"
                    ),
                )
            })
        })?
        .map_err(|err| {
            GovernanceError::new_with_message(
                ErrorType::External,
                format!("TreasuryManager.withdraw failed: {err:?}"),
            )
        })?;

    log!(
        INFO,
        "TreasuryManager.withdraw succeeded with response: {:?}",
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

**File:** rs/sns/governance/src/extensions.rs (L1733-1746)
```rust
impl TryFrom<Option<Precise>> for ValidatedWithdrawOperationArg {
    type Error = String;

    fn try_from(value: Option<Precise>) -> Result<Self, Self::Error> {
        let original = value.unwrap_or_default();

        // For now, only allow empty arguments
        // This ensures withdraw operations don't accept parameters yet
        if original.value.is_some() {
            return Err("Withdraw operation does not accept arguments at this time".to_string());
        }

        Ok(Self { original })
    }
```

**File:** rs/sns/governance/src/governance/assorted_governance_tests.rs (L4931-4971)
```rust
        },
        TopicInfo {
            topic: Topic::TreasuryAssetManagement,
            name: "Treasury & asset management".to_string(),
            description: "Proposals to move and manage assets that are DAO-owned, including tokens in the treasury, tokens in liquidity pools, or DAO-owned neurons.".to_string(),
            functions: NervousSystemFunctions {
                native_functions: vec![
                    NervousSystemFunction {
                        id: 9,
                        name: "Transfer SNS treasury funds".to_string(),
                        description: Some(
                            "Proposal to transfer funds from an SNS Governance controlled treasury account".to_string(),
                        ),
                        function_type: Some(
                            FunctionType::NativeNervousSystemFunction(
                                Empty {},
                            ),
                        ),
                    },
                    NervousSystemFunction {
                        id: 12,
                        name: "Mint SNS tokens".to_string(),
                        description: Some(
                            "Proposal to mint SNS tokens to a specified recipient.".to_string(),
                        ),
                        function_type: Some(
                            FunctionType::NativeNervousSystemFunction(
                                Empty {},
                            ),
                        ),
                    },
                ],
                custom_functions: vec![],
            },
            extension_operations: vec![
                RegisteredExtensionOperationSpec { canister_id: CanisterId::from_u64(100_001), spec:  deposit_operation_spec.clone() },
                RegisteredExtensionOperationSpec { canister_id: CanisterId::from_u64(100_001), spec:  withdraw_operation_spec.clone() },
                RegisteredExtensionOperationSpec { canister_id: CanisterId::from_u64(100_002), spec:  deposit_operation_spec },
                RegisteredExtensionOperationSpec { canister_id: CanisterId::from_u64(100_002), spec:  withdraw_operation_spec },
            ],
            is_critical: true,
```
