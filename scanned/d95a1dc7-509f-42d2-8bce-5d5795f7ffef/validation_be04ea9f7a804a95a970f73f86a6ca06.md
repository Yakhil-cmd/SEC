### Title
Missing Slippage Protection in SNS Treasury Manager Deposit API — (`rs/sns/treasury_manager/treasury_manager.did`, `rs/sns/governance/src/extensions.rs`)

---

### Summary

The SNS Treasury Manager `DepositRequest` type contains no minimum-output (slippage) field, and the SNS Governance execution path for `execute_treasury_manager_deposit` does not validate the returned balances against any minimum. This is structurally identical to the Particle bug: a liquidity deposit into an on-chain DEX is executed with no enforceable floor on the LP tokens received, leaving the SNS treasury exposed to price manipulation between governance proposal approval and execution.

---

### Finding Description

The `DepositRequest` struct in `rs/sns/treasury_manager/src/lib.rs` contains only `allowances` — the amounts approved for the Treasury Manager to spend:

```rust
pub struct DepositRequest {
    pub allowances: Vec<Allowance>,
}
``` [1](#0-0) 

There is no `minimum_lp_tokens_received`, `minimum_token0_deposited`, or any equivalent slippage-protection field. The Candid interface confirms the same: [2](#0-1) 

The `treasury_manager.did` itself explicitly acknowledges this as a known risk:

> Some liquidity pools do not implement slippage protection for deposits. In other words, the price ratio at the time of execution may differ from the ratio at the time the proposal was approved. [3](#0-2) 

The governance execution path in `execute_treasury_manager_deposit` (`rs/sns/governance/src/extensions.rs`) approves the Treasury Manager to spend the full `treasury_allocation_sns_e8s` and `treasury_allocation_icp_e8s`, calls `deposit`, and only **logs** the returned `balances` — it performs no validation of the returned balances against any minimum: [4](#0-3) 

The proposal validation step (`validate_deposit_operation_impl`) only checks that the requested amounts do not exceed 50% of the current treasury balance — it does not check any minimum output: [5](#0-4) 

The proposal rendering in `rs/sns/governance/src/proposal.rs` even warns voters about this, but the warning is informational only — no enforcement exists in the execution path: [6](#0-5) 

---

### Impact Explanation

An SNS governance proposal to deposit treasury funds into a DEX via a Treasury Manager takes days to pass. During the voting period and the window between approval and execution, any canister or user that can trade on the target DEX can manipulate the pool price. When `execute_treasury_manager_deposit` fires, the SNS treasury receives far fewer LP tokens than the price at proposal-creation time implied. Because the `DepositRequest` has no minimum-output field and the governance code does not validate returned balances, there is no on-chain mechanism to abort the deposit if the price has moved adversely. The result is a direct, irreversible financial loss to the SNS treasury — SNS tokens and ICP are permanently deposited at a manipulated price.

---

### Likelihood Explanation

The IC does not have Ethereum-style mempool frontrunning, but the multi-day governance voting window creates a large, predictable time gap between price observation (proposal creation) and execution. Any participant who can observe the governance proposal (all proposals are public) and trade on the DEX can shift the pool price before execution. The attack requires no privileged access, no key compromise, and no subnet-majority corruption — only the ability to trade on the same DEX the Treasury Manager targets. As SNS treasuries grow and Treasury Manager integrations are deployed, the financial incentive to execute this attack increases proportionally.

---

### Recommendation

1. Add a `minimum_lp_tokens_received` (or equivalent per-asset minimum output) field to `DepositRequest` in `rs/sns/treasury_manager/src/lib.rs` and `treasury_manager.did`.
2. Require the SNS governance deposit proposal to include user-specified minimum output values, validated in `validate_deposit_operation_impl`.
3. In `execute_treasury_manager_deposit`, after calling `deposit`, compare the returned `external_custodian` balance in `Balances` against the governance-specified minimum and revert (or trigger a withdrawal) if the minimum is not met.
4. Consider adding a deadline parameter to the `DepositRequest` so that a deposit cannot be executed after a governance-specified timestamp.

---

### Proof of Concept

1. SNS governance submits a `ExecuteExtensionOperation` proposal with `operation_name = "deposit"` and `treasury_allocation_sns_e8s = X`, `treasury_allocation_icp_e8s = Y` targeting a DEX-backed Treasury Manager.
2. The proposal enters the multi-day voting period. The current DEX price implies the SNS should receive `Z` LP tokens.
3. An attacker observes the proposal and executes large trades on the DEX, shifting the pool price adversely (e.g., draining one side of the pool).
4. The proposal passes and `execute_treasury_manager_deposit` fires:
   - `approve_treasury_manager` grants the Treasury Manager an ICRC-2 allowance for the full `X` SNS tokens and `Y` ICP.
   - `call_canister(..., "deposit", arg_blob)` is called with a `DepositRequest` containing only the allowances — no minimum output.
   - The Treasury Manager calls the DEX at the manipulated price and deposits the tokens, receiving `Z' << Z` LP tokens.
   - The returned `balances` are logged but not validated.
5. The SNS treasury has permanently lost value: `X` SNS tokens and `Y` ICP were deposited but only `Z'` LP tokens were received instead of the expected `Z`. [7](#0-6) [1](#0-0) [3](#0-2)

### Citations

**File:** rs/sns/treasury_manager/src/lib.rs (L284-287)
```rust
#[derive(CandidType, Clone, Debug, Deserialize, PartialEq)]
pub struct DepositRequest {
    pub allowances: Vec<Allowance>,
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

**File:** rs/sns/governance/src/proposal.rs (L1540-1545)
```rust
## WARNING

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.
```
