### Title
No Slippage Protection for SNS Treasury Manager DEX Deposits - (File: `rs/sns/governance/src/extensions.rs`)

### Summary
The SNS governance `execute_treasury_manager_deposit` function approves and forwards SNS/ICP treasury funds to a treasury manager canister for deposit into a DEX liquidity pool without enforcing any minimum LP-tokens-out (slippage) constraint. The `DepositRequest` type carries only token allowances and no `min_lp_tokens_out` field. An attacker who observes a pending governance proposal can sandwich the deposit, imbalancing the DEX pool before execution and extracting value from the SNS treasury.

### Finding Description
When an SNS governance proposal of type `ExecuteExtensionOperation` / `TreasuryManagerDeposit` is adopted, `execute_treasury_manager_deposit` is called:

1. It calls `approve_treasury_manager`, granting the treasury manager canister an ICRC-2 allowance for the exact `treasury_allocation_sns_e8s` and `treasury_allocation_icp_e8s` amounts specified in the proposal.
2. It then calls `deposit` on the treasury manager canister, passing a `DepositRequest` that contains only `allowances` (the token amounts and refund accounts). [1](#0-0) 

The `DepositRequest` type defined in the treasury manager interface has no field for a minimum acceptable LP token output: [2](#0-1) 

The codebase itself acknowledges this gap as a "Known Security Risk": [3](#0-2) 

A slippage warning is emitted only in the proposal rendering for `RegisterExtension`, not for `ExecuteExtensionOperation`: [4](#0-3) 

The `validate_and_render_execute_extension_operation` function that renders the actual deposit proposal shows no such warning: [5](#0-4) 

The validation step (`validate_deposit_operation_impl`) only checks that the requested amounts do not exceed 50% of the current treasury balance; it performs no price-ratio or slippage check: [6](#0-5) 

### Impact Explanation
An attacker can front-run the publicly observable governance proposal execution by swapping tokens into the target DEX pool to skew the price ratio. When `execute_treasury_manager_deposit` fires, the treasury manager deposits at the manipulated ratio, receiving far fewer LP tokens than the ratio implied at proposal-creation time. The attacker then back-runs to restore the pool and pocket the profit. Because the `DepositRequest` carries no `min_lp_tokens_out` guard, neither the governance canister nor the treasury manager can reject the unfavorable execution. The SNS treasury suffers a direct, permanent loss of value proportional to the attacker's price impact.

### Likelihood Explanation
SNS governance proposals are public and have a known voting period (typically days), giving an attacker ample time to prepare. The attacker needs only enough capital to meaningfully imbalance the target DEX pool. No privileged access, key compromise, or subnet-majority corruption is required. The attack is fully executable by an unprivileged canister caller or boundary-node user who can submit transactions to the DEX and observe the IC mempool/governance state.

### Recommendation
1. Add a `min_lp_tokens_out : opt nat` field to `DepositRequest` in `treasury_manager.did` so that treasury manager implementations can enforce a slippage bound at the DEX call site.
2. Propagate the bound through `execute_treasury_manager_deposit` and `construct_treasury_manager_deposit_payload` so the governance-approved minimum is passed to the treasury manager.
3. Add the slippage warning to `validate_and_render_execute_extension_operation` (not only to `validate_and_render_register_extension`) so that voters approving a deposit proposal are explicitly informed of the risk.

### Proof of Concept
1. An SNS adopts a `TreasuryManagerDeposit` proposal allocating X SNS tokens and Y ICP to a DEX liquidity pool.
2. Attacker observes the adopted proposal on-chain (governance state is public).
3. Attacker submits a large swap on the target DEX pool, shifting the SNS/ICP price ratio significantly.
4. IC consensus executes the governance proposal: `execute_treasury_manager_deposit` approves the treasury manager and calls `deposit` with the original `treasury_allocation_sns_e8s` / `treasury_allocation_icp_e8s` amounts and no slippage bound.
5. The treasury manager deposits into the DEX at the manipulated ratio; the SNS treasury receives LP tokens worth substantially less than X SNS + Y ICP at the pre-attack price.
6. Attacker swaps back, restoring the pool and realizing the profit extracted from the SNS treasury. [7](#0-6) [8](#0-7)

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

**File:** rs/sns/governance/src/extensions.rs (L1037-1065)
```rust
fn construct_treasury_manager_deposit_allowances(
    context: TreasuryManagerDepositContext,
    value: Precise,
) -> Result<Vec<Allowance>, String> {
    // See ic_sns_init::distributions::FractionalDeveloperVotingPower.insert_treasury_accounts
    let (treasury_sns_subaccount, treasury_icp_subaccount) = treasury_subaccounts(context.clone());

    let allowances = treasury_manager::construct_deposit_allowances(
        value,
        Asset::Token {
            symbol: context.sns_token_symbol,
            ledger_canister_id: context.sns_ledger_canister_id.get().0,
            ledger_fee_decimals: Nat::from(context.sns_ledger_transaction_fee_e8s),
        },
        Asset::Token {
            symbol: "ICP".to_string(),
            ledger_canister_id: context.icp_ledger_canister_id.get().0,
            ledger_fee_decimals: Nat::from(icp_ledger::DEFAULT_TRANSFER_FEE.get_e8s()),
        },
        sns_treasury_manager::Account {
            owner: context.sns_governance_canister_id.get().0,
            subaccount: treasury_sns_subaccount,
        },
        sns_treasury_manager::Account {
            owner: context.sns_governance_canister_id.get().0,
            subaccount: treasury_icp_subaccount,
        },
    )
    .map_err(|err| format!("Error extracting initial allowances: {err}"))?;
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

**File:** rs/sns/treasury_manager/treasury_manager.did (L84-86)
```text
type DepositRequest = record {
  allowances : vec Allowance;
};
```

**File:** rs/sns/governance/src/proposal.rs (L1484-1504)
```rust
async fn validate_and_render_execute_extension_operation(
    governance: &crate::governance::Governance,
    execute: &ExecuteExtensionOperation,
) -> Result<String, String> {
    let ValidatedExecuteExtensionOperation {
        extension_canister_id,
        operation_name,
        arg,
    } = validate_execute_extension_operation(governance, execute.clone())
        .await
        .map_err(|err| err.error_message)?;

    Ok(format!(
        r"# Proposal to execute extension operation:

* Extension canister ID: `{extension_canister_id}`
* Operation name: `{operation_name}`
* Operation argument: `{arg}`
#"
    ))
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
