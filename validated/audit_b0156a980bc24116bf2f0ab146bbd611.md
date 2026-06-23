### Title
Losses from DEX Slippage During SNS Treasury Manager Withdrawal Are Not Accounted For in the Governance Proposal Execution - (File: rs/sns/governance/src/extensions.rs)

### Summary
The `execute_treasury_manager_withdraw` function in the SNS Governance canister calls the Treasury Manager's `withdraw` endpoint and logs the returned `Balances` response, but does not verify or enforce that the amount returned to the SNS treasury matches the amount originally deposited. Losses incurred due to DEX slippage, impermanent loss, or partial withdrawal failures are silently accepted and never surfaced to governance or token holders as a loss event. This is the direct IC analog of the Liquity `liquidatePosition()` bug: a withdrawal-triggered loss is ignored in the accounting path.

### Finding Description
In `rs/sns/governance/src/extensions.rs`, the function `execute_treasury_manager_withdraw` (lines 1612–1661) calls the external Treasury Manager canister's `withdraw` method and decodes the `TreasuryManagerResult`. If the call succeeds (returns `Ok(Balances)`), the function simply logs the balances and returns `Ok(())` to the governance proposal executor — with no check that the returned `treasury_owner` balance in the `Balances` response reflects the full expected amount.

The `treasury_manager.did` interface (lines 143–172) defines a `BalanceBook` invariant:
```
managed_assets[k] == managed_assets[k-1] + payers[k] - payees[k] - fee_collector[k]
```
This invariant can be violated by DEX slippage or impermanent loss during withdrawal. The `treasury_manager.did` itself explicitly acknowledges this risk at lines 35–40:
> "Some liquidity pools do not implement slippage protection for deposits. In other words, the price ratio at the time of execution may differ from the ratio at the time the proposal was approved."

The same risk applies to withdrawals: a DEX can return fewer tokens than deposited. The governance canister's `execute_treasury_manager_withdraw` does not compare the pre-withdrawal treasury balance against the post-withdrawal balance, nor does it emit any governance event or proposal failure when the returned amount is less than expected.

The `execute_treasury_manager_deposit` function (lines 1545–1610) has the same pattern: it calls `deposit`, decodes the result, logs it, and returns `Ok(())` without verifying that the deposited amount was fully accepted.

The proposal rendering in `validate_and_render_register_extension` (lines 1527–1550 of `rs/sns/governance/src/proposal.rs`) does warn about slippage for deposits, but no analogous warning or enforcement exists for withdrawals, and no on-chain accounting check is performed in either case.

### Impact Explanation
An SNS DAO that deposits treasury funds (SNS tokens + ICP) into a DEX liquidity pool via a Treasury Manager extension, then later votes to withdraw, may receive fewer tokens back than deposited due to:
- DEX slippage (price ratio changed between deposit and withdrawal)
- Impermanent loss
- Partial withdrawal failures silently accepted by the Treasury Manager

The governance canister will mark the withdrawal proposal as **successfully executed** (`failure_reason = None`) even when the treasury has suffered a net loss. Token holders have no on-chain signal that a loss occurred. The `Balances` response is only logged (not stored in proposal state), so there is no queryable record of the discrepancy. This leads to improper accounting of DAO-owned assets and potential locking of future treasury operations if the DAO's internal accounting diverges from actual ledger balances.

**Impact**: Ledger conservation bug / governance authorization accounting bug — the SNS treasury's actual token balance after withdrawal may be less than what governance records imply, with no on-chain loss event emitted.

### Likelihood Explanation
The SNS Treasury Manager extension is a new, actively developed feature. Any SNS that registers a Treasury Manager extension pointing to a DEX (e.g., KongSwap, as tested in `rs/nervous_system/integration_tests/tests/sns_extension_test.rs`) and executes a `withdraw` proposal is exposed. The `treasury_manager.did` explicitly documents slippage as a known risk. The likelihood is **medium**: it requires an SNS to have deployed a Treasury Manager extension and executed at least one deposit+withdraw cycle, but no attacker action is required — normal market price movement is sufficient to trigger the loss.

### Recommendation
1. In `execute_treasury_manager_withdraw`, after a successful `withdraw` call, compare the `treasury_owner` balance in the returned `Balances` against the pre-withdrawal balance (queried before the call). If the returned amount is less than expected by more than a configurable slippage tolerance, either revert the proposal as failed or emit a dedicated governance event recording the loss amount.
2. Store the post-withdrawal `Balances` in the proposal's `action_auxiliary` or a dedicated field so it is queryable on-chain.
3. Add the same slippage warning to the `validate_and_render_execute_extension_operation` rendering for `withdraw` operations (currently only present for `RegisterExtension`).
4. Consider requiring Treasury Manager implementations to expose a minimum-return parameter in `WithdrawRequest` that the governance canister enforces before calling `withdraw`.

### Proof of Concept

**Entry path**: Any unprivileged SNS neuron holder with sufficient voting power can submit and pass an `ExecuteExtensionOperation` proposal with `operation_name = "withdraw"`. This is a governance-authorized but unprivileged ingress path — no admin key or privileged role is needed beyond normal neuron voting.

**Execution flow**:

1. SNS governance executes the passed proposal via `perform_execute_extension_operation` → `ValidatedExecuteExtensionOperation::execute` → `execute_treasury_manager_withdraw`. [1](#0-0) 

2. The function calls `extension_canister_id.withdraw(arg_blob)` and decodes the result. If `Ok(balances)`, it logs and returns `Ok(())` — no loss check. [2](#0-1) 

3. The proposal is marked as executed with `failure_reason = None`. [3](#0-2) 

4. The `BalanceBook` invariant in the Treasury Manager DID explicitly allows `suspense != 0` during transient errors, and the `managed_assets` invariant can be violated by DEX slippage. [4](#0-3) 

5. The known slippage risk is documented but not enforced on-chain. [5](#0-4) 

6. The proposal rendering for `RegisterExtension` warns about slippage but `execute_treasury_manager_withdraw` has no equivalent enforcement. [6](#0-5)

### Citations

**File:** rs/sns/governance/src/extensions.rs (L1612-1661)
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
}
```

**File:** rs/sns/governance/src/governance.rs (L2176-2179)
```rust
            Action::ExecuteExtensionOperation(execute_extension_operation) => {
                self.perform_execute_extension_operation(execute_extension_operation)
                    .await
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

**File:** rs/sns/treasury_manager/treasury_manager.did (L143-172)
```text
/// Let `k` denote a particular state, `party[k]` denote the account balance of `party`
/// in state `k`, and `managed_assets` be the sum of all assets managed on behalf of
/// the treasury owner in state `k`.
///
/// Initial managed assets
/// ----------------------
/// managed_assets[0] == treasury_manager[0]
///
///     (treasury_owner[0] == external_custodian[0] == fee_collector[0]
///      == payees[0] == payers[0] == suspense[0] == 0)
///
/// Current managed assets
/// ----------------------
/// managed_assets[k] == treasury_manager[k] + treasury_owner[k] + external_custodian[k]
///
/// Under "normal operations", the following invariants hold for all k > 0:
/// 1) suspense[k] == 0
/// 2) managed_assets[k] == managed_assets[k-1] + payers[k] - payees[k] - fee_collector[k]
type BalanceBook = record {
  treasury_owner : opt Balance;
  treasury_manager : opt Balance;
  external_custodian : opt Balance;
  fee_collector : opt Balance;
  payees : opt Balance;
  payers : opt Balance;

  // An account in which items are entered temporarily before allocation to the correct
  // or final account, e.g., due to transient errors.
  suspense : opt Balance;
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
