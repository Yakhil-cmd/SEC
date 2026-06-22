### Title
Missing Slippage Protection in SNS Treasury Manager Deposit Execution - (File: `rs/sns/governance/src/extensions.rs`)

### Summary
The SNS governance framework for treasury management deposits (`ExecuteExtensionOperation` / `TreasuryManagerDeposit`) fixes deposit amounts at proposal-creation time and provides no mechanism to enforce price bounds or slippage tolerance at execution time. Any actor that can interact with the target DEX can front-run or sandwich the governance-triggered deposit, causing the SNS treasury to deposit at an unfavorable token ratio and suffer a direct financial loss.

### Finding Description

When an SNS governance proposal of type `ExecuteExtensionOperation` with operation `deposit` is adopted, the execution path is:

1. `perform_execute_extension_operation` → `validate_execute_extension_operation` → `execute_treasury_manager_deposit`

The deposit amounts (`treasury_allocation_sns_e8s`, `treasury_allocation_icp_e8s`) are encoded in the proposal payload at submission time and are not re-evaluated against current market conditions at execution time. [1](#0-0) 

The only validation performed at execution time is `validate_deposit_operation_impl`, which checks that the requested amounts do not exceed 50% of the current treasury balance — it does not check the current market price or enforce any price bounds: [2](#0-1) 

The `ExecuteExtensionOperation` proto type carries no slippage fields whatsoever: [3](#0-2) 

The codebase itself acknowledges this gap in two places. In the proposal rendering for `RegisterExtension`: [4](#0-3) 

And in the treasury manager interface specification: [5](#0-4) 

The acknowledgment is only a UI warning; no enforcement exists in the execution path.

### Impact Explanation

An attacker who monitors the SNS governance canister for adopted `TreasuryManagerDeposit` proposals can, before the governance canister executes the deposit (or in the same consensus round), manipulate the DEX pool price. The governance canister then calls `approve_treasury_manager` granting ICRC-2 allowances for the fixed amounts, and calls `deposit` on the treasury manager, which forwards those fixed amounts to the DEX at the now-manipulated price. The attacker then reverses the price manipulation and profits from the spread. The SNS treasury receives fewer LP tokens than it should, representing a direct, irreversible financial loss of up to 50% of the treasury per proposal (the maximum allowed by the 50% balance check). Undeposited tokens are returned, but the deposited portion is locked at the unfavorable ratio.

### Likelihood Explanation

The SNS extensions feature is gated by `is_sns_extensions_enabled()` and is not yet live as of the current codebase: [6](#0-5) 

However, the code is in production and the feature is explicitly planned for enablement. Once enabled, any SNS that registers a DEX-backed treasury manager extension is immediately exposed. The attack requires: (1) monitoring the governance canister for adopted proposals (public information), (2) capital to manipulate the DEX pool, and (3) timing the manipulation before the governance heartbeat/timer fires the execution. On the IC, inter-canister calls are ordered by consensus, and the governance execution is triggered by a periodic task, making the timing window predictable. Likelihood is **Medium** once the feature is enabled.

### Recommendation

Add slippage parameters to the `ExecuteExtensionOperation` proposal type (e.g., `min_sns_per_icp_e8s` / `max_sns_per_icp_e8s`, or a `max_slippage_bps` field). In `execute_treasury_manager_deposit`, query the current DEX price before calling `approve_treasury_manager` and abort with an error if the price has moved outside the caller-specified bounds. Alternatively, pass the slippage parameters through to the treasury manager's `deposit` call so the treasury manager can enforce them on-chain at the DEX level. The `DepositRequest` type in `treasury_manager.did` should be extended to carry these bounds. [7](#0-6) 

### Proof of Concept

1. An SNS submits a `TreasuryManagerDeposit` proposal specifying `treasury_allocation_icp_e8s = 1_000_000_000` (10 ICP) and `treasury_allocation_sns_e8s = 500_000_000` (5 SNS tokens) to deposit into a KongSwap pool at the current market ratio of 2 ICP per SNS token.
2. The proposal passes governance voting (days later). The market ratio is still 2:1.
3. An attacker observes the proposal is adopted and about to execute. The attacker calls the DEX directly to buy a large amount of SNS tokens, pushing the pool price to 10 ICP per SNS token.
4. The governance canister's periodic task fires `perform_execute_extension_operation` → `execute_treasury_manager_deposit`. The function calls `approve_treasury_manager` for the fixed amounts and then calls `deposit` on the treasury manager.
5. The treasury manager deposits 10 ICP and 5 SNS tokens into the pool at the manipulated 10:1 ratio. At the true 2:1 ratio, 10 ICP should have been paired with only 1 SNS token; the SNS treasury has effectively over-contributed 4 SNS tokens worth of value.
6. The attacker sells their SNS tokens back into the pool, restoring the price and pocketing the arbitrage profit at the SNS treasury's expense.
7. No check in `execute_treasury_manager_deposit` or `validate_deposit_operation_impl` detects or prevents this. [8](#0-7)

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

**File:** rs/sns/governance/canister/governance.did (L776-782)
```text
type ExecuteExtensionOperation = record {
  extension_canister_id : opt principal;

  operation_name : opt text;

  operation_arg : opt ExtensionOperationArg;
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

**File:** rs/sns/governance/src/governance.rs (L2558-2576)
```rust
    async fn perform_execute_extension_operation(
        &self,
        execute_extension_operation: ExecuteExtensionOperation,
    ) -> Result<(), GovernanceError> {
        // Check if SNS extensions are enabled
        if !crate::is_sns_extensions_enabled() {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "SNS extensions are not enabled",
            ));
        }

        let validated_operation =
            validate_execute_extension_operation(self, execute_extension_operation).await?;

        // Execute the validated operation
        validated_operation.execute(self).await?;

        Ok(())
```
