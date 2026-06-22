### Title
SNS Treasury Manager Deposit Lacks Slippage Protection, Enabling Sandwich Attacks on DAO Treasury - (File: rs/sns/treasury_manager/treasury_manager.did / rs/sns/governance/src/extensions.rs)

### Summary
The SNS governance framework's treasury deposit flow (`execute_treasury_manager_deposit`) transfers SNS and ICP tokens from the DAO treasury into an external DEX liquidity pool without enforcing any slippage or minimum-received-LP-tokens check. The `treasury_manager.did` interface specification explicitly acknowledges this as a known security risk. An unprivileged attacker who can trade on the same DEX can sandwich the governance-triggered deposit, causing the SNS treasury to receive fewer LP tokens than expected and permanently losing value.

### Finding Description
When an SNS governance proposal to deposit treasury assets is executed, the function `execute_treasury_manager_deposit` in `rs/sns/governance/src/extensions.rs` performs the following steps:

1. Calls `validate_deposit_operation_impl` which only checks that the requested amounts do not exceed 50% of the current treasury balance.
2. Approves the treasury manager canister to spend the tokens via `approve_treasury_manager`.
3. Calls `deposit` on the external treasury manager canister (a DEX integration) with no price-ratio or minimum-LP-output constraint. [1](#0-0) 

The pre-execution validation in `validate_deposit_operation_impl` only enforces a 50% cap on the requested amounts relative to the current treasury balance. It does not record the current DEX price at proposal creation time, nor does it enforce any minimum acceptable exchange rate at execution time. [2](#0-1) 

The `treasury_manager.did` interface specification explicitly documents this gap under "Known Security Risks":

> "Some liquidity pools do not implement slippage protection for deposits. In other words, the price ratio at the time of execution may differ from the ratio at the time the proposal was approved." [3](#0-2) 

This is structurally identical to the Uniswap v4-periphery bug: in that case, `validateMaxInNegative` skipped the slippage check when `balanceDelta` was positive (fees exceeded deposit). Here, the IC analog is that the entire slippage check is absent — the combined token amount approved to the DEX is never validated against a minimum LP output.

### Impact Explanation
An attacker can execute a classic sandwich attack against any SNS DAO that uses the Treasury Manager extension:

1. Monitor the IC governance canister for a pending `ExecuteExtensionOperation` deposit proposal that is about to be executed.
2. Before execution, submit large trades on the target DEX to move the price adversely (e.g., buy SNS tokens to inflate their price relative to ICP, or vice versa).
3. The governance canister executes the deposit at the manipulated price. The DAO receives significantly fewer LP tokens than the fair-market value of the deposited assets.
4. The attacker reverses their trades, profiting from the price impact while the DAO treasury permanently loses value.

The loss is proportional to the deposit size (up to 50% of the treasury per proposal) and the attacker's ability to move the DEX price. For large SNS treasuries, this could represent millions of dollars in value.

### Likelihood Explanation
- The attack requires no privileged access — any user who can trade on the target DEX can execute it.
- SNS governance proposals have a multi-day voting period, giving attackers ample time to prepare.
- The execution of a governance proposal is a public, observable on-chain event, making the timing predictable.
- The `treasury_manager.did` specification itself acknowledges this risk, indicating it is a known, unmitigated issue in the current design.
- Likelihood is **medium-high** for any SNS that deploys a Treasury Manager pointing to a DEX with sufficient liquidity for price manipulation.

### Recommendation
1. **Enforce slippage at the governance layer**: The `ValidatedDepositOperationArg` struct should include caller-specified minimum LP token output fields (e.g., `min_lp_tokens_out`). The `validate_deposit_operation_impl` function should require these fields to be present and non-zero.
2. **Record price at proposal creation**: Capture the DEX price ratio at proposal submission time and enforce a maximum acceptable deviation at execution time.
3. **Enforce slippage at the Treasury Manager layer**: The `deposit` function in the Treasury Manager canister should accept and enforce a `min_lp_out` parameter, rejecting the deposit if the DEX would return fewer LP tokens than specified.
4. **Add a time-lock or execution window**: Limit the window between proposal approval and execution to reduce the attacker's preparation time.

### Proof of Concept
1. An SNS DAO has a treasury with 1,000,000 SNS tokens and 500,000 ICP.
2. A governance proposal is submitted and approved to deposit 500,000 SNS + 250,000 ICP into a DEX liquidity pool.
3. The proposal enters the execution queue. An attacker observes this on-chain.
4. The attacker buys a large amount of SNS tokens on the DEX, driving up the SNS/ICP price by 20%.
5. `execute_treasury_manager_deposit` is called. It calls `approve_treasury_manager` and then `deposit` on the DEX with no minimum LP output constraint. [4](#0-3) 

6. The DEX executes the deposit at the manipulated 20%-inflated SNS price. The DAO receives ~17% fewer LP tokens than it would have at the fair price.
7. The attacker sells their SNS tokens back, profiting from the price impact. The DAO treasury has permanently lost ~17% of the deposited value with no recourse.

### Citations

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

**File:** rs/sns/treasury_manager/treasury_manager.did (L35-40)
```text
// Known Security Risks:
// ====================
// Some liquidity pools do not implement slippage protection
// for deposits. In other words, the price ratio at the time
// of execution may differ from the ratio at the time the proposal was
// approved.
```
