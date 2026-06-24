### Title
SNS Treasury Manager Deposit Lacks Minimum LP Output Enforcement, Exposing DAO Treasury to Price Manipulation - (File: rs/sns/governance/src/extensions.rs)

### Summary

The SNS Governance canister's `execute_treasury_manager_deposit` function approves and deposits SNS treasury funds into a DEX-backed Treasury Manager without enforcing any minimum LP token output or slippage bound. The `DepositRequest` API type provides no field for callers to specify a minimum acceptable output. Between proposal adoption and execution (a window of days), any actor who can interact with the target DEX can manipulate the pool price so that the treasury receives far fewer LP tokens than the DAO voted to accept. The codebase itself acknowledges this as a "Known Security Risk" but provides no protocol-level mitigation.

### Finding Description

The `execute_treasury_manager_deposit` function in `rs/sns/governance/src/extensions.rs` executes in two steps:

1. `approve_treasury_manager` — grants an ICRC-2 allowance of `treasury_allocation_sns_e8s` SNS tokens and `treasury_allocation_icp_e8s` ICP to the Treasury Manager canister.
2. Calls `deposit` on the Treasury Manager canister with a `DepositRequest` containing only the input `allowances`. [1](#0-0) 

The `DepositRequest` type defined in the Treasury Manager DID contains only `allowances` (input amounts) and no `min_lp_tokens_out` or equivalent slippage parameter: [2](#0-1) 

After the `deposit` call returns, the governance canister only logs the `balances` response — it performs no check that the LP tokens received meet any minimum threshold: [3](#0-2) 

The proposal validation step (`validate_deposit_operation_impl`) only checks that the requested input amounts do not exceed 50% of the current treasury balance. It does not validate any expected output: [4](#0-3) 

The codebase explicitly acknowledges this gap in two places. In the Treasury Manager DID: [5](#0-4) 

And in the rendered proposal warning text in `proposal.rs`: [6](#0-5) 

The warning claims "any undeposited tokens are automatically returned to the SNS treasury account," but this is a property of the Treasury Manager implementation, not enforced by the governance layer. The governance canister accepts a successful `deposit` response regardless of how many LP tokens were actually minted.

### Impact Explanation

An SNS DAO that votes to deposit treasury funds (SNS tokens + ICP) into a DEX liquidity pool via a Treasury Manager extension can have its treasury drained of value. The attacker manipulates the DEX pool price between proposal adoption and execution, causing the treasury to receive a fraction of the LP tokens the DAO intended. The SNS treasury suffers a direct, permanent loss of value proportional to the price impact of the manipulation. Because SNS governance voting periods span days, the attack window is large and predictable.

### Likelihood Explanation

The attack window is the entire governance voting period — typically multiple days — which is publicly observable on-chain. Any actor who can interact with the target DEX (an unprivileged canister caller or user) can execute the manipulation. The proposal execution is deterministic and cannot be cancelled once adopted. The IC's lack of a traditional mempool does not protect against this attack because the manipulation occurs during the voting period, not at the moment of execution. The `DepositRequest` API provides no mechanism for the DAO to express a minimum acceptable output, so there is no protocol-level defense even if the Treasury Manager implementation wanted to enforce one.

### Recommendation

1. **Extend `DepositRequest`** in `rs/sns/treasury_manager/treasury_manager.did` to include a `min_lp_tokens_out : opt nat` field (or equivalent per-asset minimum output fields).
2. **Extend `ValidatedDepositOperationArg`** in `rs/sns/governance/src/extensions.rs` to carry and validate a minimum LP output parameter specified in the proposal.
3. **In `execute_treasury_manager_deposit`**, after the `deposit` call returns, decode the `Balances` response and verify that the `external_custodian` balance meets the minimum LP token threshold specified in the proposal. Revert (and attempt to withdraw) if the threshold is not met.
4. **Enforce a maximum time-to-execution** for treasury deposit proposals to bound the manipulation window.

### Proof of Concept

1. An SNS DAO submits an `ExecuteExtensionOperation` proposal with `operation_name = "deposit"`, specifying `treasury_allocation_sns_e8s = X` and `treasury_allocation_icp_e8s = Y` to deposit into a DEX pool via a registered Treasury Manager.
2. The proposal enters the voting period (e.g., 4 days). The target DEX pool and the proposal's execution block are publicly known.
3. An attacker, observing the pending proposal, executes large trades on the DEX to skew the pool price heavily against the SNS token (e.g., dumps SNS tokens to make them cheap relative to ICP).
4. The proposal passes and `perform_execute_extension_operation` → `execute_treasury_manager_deposit` runs:
   - `approve_treasury_manager` grants the allowance at the full `X` SNS + `Y` ICP.
   - `deposit` is called; the Treasury Manager deposits at the manipulated price, receiving far fewer LP tokens than the DAO expected.
   - The governance canister logs the `balances` response and returns `Ok(())` with no minimum-output check.
5. The attacker reverses their trades on the DEX, profiting from the price impact while the SNS treasury holds LP tokens worth significantly less than the deposited assets. [7](#0-6) [8](#0-7) [5](#0-4)

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

**File:** rs/sns/governance/src/extensions.rs (L777-831)
```rust
    async fn approve_treasury_manager(
        &self,
        treasury_manager_canister_id: CanisterId,
        sns_amount_e8s: u64,
        icp_amount_e8s: u64,
    ) -> Result<(), GovernanceError> {
        let to = Account {
            owner: treasury_manager_canister_id.get().0,
            subaccount: None,
        };

        let expiry_time_sec = self.env.now().saturating_add(ONE_HOUR_SECONDS);
        let expiry_time_nsec = expiry_time_sec.saturating_mul(NANO_SECONDS_PER_SECOND);

        // If expected_allowance is None, the ledger *blindly* overwrites any existing
        // allowance (even if non-zero). Therefore, there is no risk of double spending.

        self.ledger
            .icrc2_approve(
                to,
                sns_amount_e8s,
                Some(expiry_time_nsec),
                self.transaction_fee_e8s_or_panic(),
                self.sns_treasury_subaccount(),
                None,
            )
            .await
            .map(|_| ())
            .map_err(|e| {
                GovernanceError::new_with_message(
                    ErrorType::External,
                    format!("Error making SNS Token treasury transfer: {e}"),
                )
            })?;

        self.nns_ledger
            .icrc2_approve(
                to,
                icp_amount_e8s,
                Some(expiry_time_nsec),
                icp_ledger::DEFAULT_TRANSFER_FEE.get_e8s(),
                self.icp_treasury_subaccount(),
                None,
            )
            .await
            .map(|_| ())
            .map_err(|e| {
                GovernanceError::new_with_message(
                    ErrorType::External,
                    format!("Error making ICP Token treasury transfer: {e}"),
                )
            })?;

        Ok(())
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
