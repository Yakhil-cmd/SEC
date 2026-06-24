### Title
SNS Governance Canister Cycles Are Irrecoverable — No Proposal Action Exists to Transfer Cycles or Attach Them to External Calls (`rs/sns/governance/src/canister_control.rs`, `rs/sns/governance/src/governance.rs`)

---

### Summary

The SNS Governance canister accumulates cycles (from top-ups, operational surplus, etc.) but has no governance proposal mechanism to transfer those cycles to other canisters or to attach them to outgoing inter-canister calls. The `ExecuteGenericNervousSystemFunction` execution path calls `env.call_canister()` with no cycles parameter, and `TransferSnsTreasuryFunds` only supports ICP and SNS token transfers. Cycles deposited into the SNS Governance canister are therefore permanently locked for that canister's own operational burn and cannot be directed to any external purpose via governance.

---

### Finding Description

**Root cause 1 — `perform_execute_generic_nervous_system_function_call` cannot attach cycles:** [1](#0-0) 

The function signature is `call_canister(canister_id, method, payload)` — there is no `cycles` argument. Any SNS proposal that executes a generic nervous system function will make a zero-cycles inter-canister call, regardless of how many cycles the governance canister holds.

**Root cause 2 — `TransferSnsTreasuryFunds` has no cycles variant:** [2](#0-1) 

The `perform_transfer_sns_treasury_funds` function matches only on `TransferFrom::IcpTreasury` and `TransferFrom::SnsTokenTreasury`: [3](#0-2) 

There is no `TransferFrom::CyclesTreasury` (or equivalent) variant in the proto definition: [4](#0-3) 

**Root cause 3 — No other proposal action covers cycles transfer:**

The full `perform_action` dispatch in SNS Governance covers every `Action` variant: [5](#0-4) 

None of the listed actions (`Motion`, `ManageNervousSystemParameters`, `UpgradeSnsControlledCanister`, `ExecuteGenericNervousSystemFunction`, `TransferSnsTreasuryFunds`, `MintSnsTokens`, etc.) can transfer cycles from the governance canister to another canister or attach cycles to an outgoing call.

---

### Impact Explanation

**Impact: High.** Cycles deposited into the SNS Governance canister — whether by the CMC top-up flow, by direct `deposit_cycles` management canister calls, or by any other means — cannot be recovered or redirected via governance proposals. Specifically:

1. An SNS that wants to top up one of its dapp canisters using the governance canister's cycle surplus cannot do so through any proposal.
2. An SNS that wants to call an external service (e.g., a DEX, oracle, or protocol) that requires cycles attached to the call cannot do so via `ExecuteGenericNervousSystemFunction`.
3. Excess cycles in the governance canister are silently consumed by the canister's own operational burn over time, with no community-controlled direction.

This is a direct analog of the EVM finding: assets (cycles) are deposited into the DAO canister but there is no mechanism to move them out or use them in external calls.

---

### Likelihood Explanation

**Likelihood: High.** Every SNS deployment receives an initial cycles allocation to its governance canister at creation time: [6](#0-5) 

Any SNS that is subsequently topped up (e.g., via `notify_top_up` through the CMC) or that accumulates cycles from other sources will have cycles in its governance canister that it cannot use for external purposes. The inability to attach cycles to `ExecuteGenericNervousSystemFunction` calls is a permanent structural limitation affecting all SNS DAOs.

---

### Recommendation

1. **Add a `cycles` field to `ExecuteGenericNervousSystemFunction`** (or to the `NervousSystemFunction` registration) and thread it through `perform_execute_generic_nervous_system_function_call` so that the governance canister can attach cycles to outgoing calls:

```rust
// In canister_control.rs
pub async fn perform_execute_generic_nervous_system_function_call(
    env: &dyn Environment,
    function: NervousSystemFunction,
    call: ExecuteGenericNervousSystemFunction,
    cycles: u64,  // NEW
) -> Result<(), GovernanceError> {
    ...
    let result = env
        .call_canister_with_cycles(  // NEW
            valid_function.target_canister_id,
            &valid_function.target_method,
            call.payload,
            cycles,
        )
        .await;
    ...
}
```

2. **Add a `TransferFrom::CyclesTreasury` variant** to `TransferSnsTreasuryFunds` (or a dedicated `TransferCycles` proposal action) that calls `deposit_cycles` on the management canister to top up a target canister from the governance canister's balance.

---

### Proof of Concept

1. Deploy an SNS. The governance canister receives its initial cycles allocation.
2. Top up the SNS Governance canister further via `notify_top_up` on the CMC, directing cycles to the governance canister ID.
3. Attempt to create a governance proposal using `ExecuteGenericNervousSystemFunction` that calls `deposit_cycles` on the management canister (to top up a dapp canister). The call will be made with **zero cycles attached** because `call_canister` has no cycles parameter.
4. Attempt to create a `TransferSnsTreasuryFunds` proposal with a cycles amount — this will fail at validation because no `CyclesTreasury` variant exists.
5. Observe that the cycles in the governance canister are irrecoverable via any governance action; they will only be consumed by the governance canister's own execution costs. [7](#0-6) [8](#0-7)

### Citations

**File:** rs/sns/governance/src/canister_control.rs (L277-314)
```rust
/// Executes a generic nervous system function (i.e., a non-native SNS proposal).
pub async fn perform_execute_generic_nervous_system_function_call(
    env: &dyn Environment,
    function: NervousSystemFunction,
    call: ExecuteGenericNervousSystemFunction,
) -> Result<(), GovernanceError> {
    // Get the canister id and the method against which we execute the proposal.
    let valid_function = ValidGenericNervousSystemFunction::try_from(&function)
        .map_err(|e| GovernanceError::new_with_message(ErrorType::InvalidProposal, e))?;

    let result = env
        .call_canister(
            valid_function.target_canister_id,
            &valid_function.target_method,
            call.payload,
        )
        .await;

    // Convert result.
    match result {
        Err(err) => Err(GovernanceError::new_with_message(
            ErrorType::External,
            format!("Canister method call to execute proposal failed: {err:?}"),
        )),

        Ok(_reply) => {
            // TODO: Do something with reply. E.g. store it in the proposal,
            // and/or deserialize it so that we can detect whether there was an
            // application-level error, as opposed to a communication
            // error. Detecting application error could be done as follows:
            //
            //   candid::!Decode(&reply, Result<String, String>)
            //
            // This could then be converted into a Result<(), GovernanceError>.
            // For now, any reply is considered a success.
            Ok(())
        }
    }
```

**File:** rs/sns/governance/src/governance.rs (L2139-2241)
```rust
    async fn perform_action(&mut self, proposal_id: u64, action: Action) {
        let result = match action {
            // Execution of Motion proposals is trivial.
            Action::Motion(_) => Ok(()),

            Action::ManageNervousSystemParameters(params) => {
                self.perform_manage_nervous_system_parameters(params)
            }
            Action::UpgradeSnsControlledCanister(params) => {
                self.perform_upgrade_sns_controlled_canister(proposal_id, params)
                    .await
            }
            Action::UpgradeSnsToNextVersion(_) => {
                log!(INFO, "Executing UpgradeSnsToNextVersion action",);
                let upgrade_sns_result = self
                    .perform_upgrade_to_next_sns_version_legacy(proposal_id)
                    .await;

                // If the upgrade returned `Ok(true)` that means the upgrade completed successfully
                // and the proposal can be marked as "executed". If the upgrade returned `Ok(false)`
                // that means the upgrade has successfully been kicked-off asynchronously, but not
                // completed. Governance's run_periodic_tasks logic will continuously check
                // the status of the upgrade and mark the proposal as either executed or failed.
                // So we call `return` in the `Ok(false)` branch so that
                // `set_proposal_execution_status` doesn't get called and set the proposal status
                // prematurely. If the result is `Err`, we do want to set the proposal status,
                // and passing the value through is sufficient.
                match upgrade_sns_result {
                    Ok(true) => Ok(()),
                    Ok(false) => return,
                    Err(e) => Err(e),
                }
            }
            Action::ExecuteGenericNervousSystemFunction(call) => {
                self.perform_execute_generic_nervous_system_function(call)
                    .await
            }
            Action::ExecuteExtensionOperation(execute_extension_operation) => {
                self.perform_execute_extension_operation(execute_extension_operation)
                    .await
            }
            Action::AddGenericNervousSystemFunction(nervous_system_function) => {
                self.perform_add_generic_nervous_system_function(nervous_system_function)
            }
            Action::RemoveGenericNervousSystemFunction(id) => {
                self.perform_remove_generic_nervous_system_function(id)
            }
            Action::RegisterDappCanisters(register_dapp_canisters) => {
                self.perform_register_dapp_canisters(register_dapp_canisters)
                    .await
            }
            Action::RegisterExtension(register_extension) => {
                self.perform_register_extension(register_extension).await
            }
            Action::UpgradeExtension(upgrade_extension) => {
                self.perform_upgrade_extension(upgrade_extension).await
            }
            Action::DeregisterDappCanisters(deregister_dapp_canisters) => {
                self.perform_deregister_dapp_canisters(deregister_dapp_canisters)
                    .await
            }
            Action::ManageSnsMetadata(manage_sns_metadata) => {
                self.perform_manage_sns_metadata(manage_sns_metadata)
            }
            Action::TransferSnsTreasuryFunds(transfer) => {
                let valuation =
                    get_action_auxiliary(&self.proto.proposals, ProposalId { id: proposal_id })
                        .and_then(|action_auxiliary| {
                            action_auxiliary.unwrap_transfer_sns_treasury_funds_or_err()
                        });
                self.perform_transfer_sns_treasury_funds(proposal_id, valuation, &transfer)
                    .await
            }
            Action::MintSnsTokens(mint) => self.perform_mint_sns_tokens(mint).await,
            Action::ManageLedgerParameters(manage_ledger_parameters) => {
                self.perform_manage_ledger_parameters(proposal_id, manage_ledger_parameters)
                    .await
            }
            Action::ManageDappCanisterSettings(manage_dapp_canister_settings) => {
                self.perform_manage_dapp_canister_settings(manage_dapp_canister_settings)
                    .await
            }
            Action::AdvanceSnsTargetVersion(_) => {
                get_action_auxiliary(&self.proto.proposals, ProposalId { id: proposal_id })
                    .and_then(|action_auxiliary| {
                        action_auxiliary.unwrap_advance_sns_target_version_or_err()
                    })
                    .and_then(|new_target| self.perform_advance_target_version(new_target))
            }
            Action::SetTopicsForCustomProposals(set_topics_for_custom_proposals) => {
                self.perform_set_topics_for_custom_proposals(set_topics_for_custom_proposals)
            }
            // This should not be possible, because Proposal validation is performed when
            // a proposal is first made.
            Action::Unspecified(_) => Err(GovernanceError::new_with_message(
                ErrorType::InvalidProposal,
                format!(
                    "A Proposal somehow made it all the way to execution despite being \
                         invalid for having its `unspecified` field populated. action: {action:?}"
                ),
            )),
        };

```

**File:** rs/sns/governance/src/governance.rs (L2980-2060)
```rust

```

**File:** rs/sns/governance/src/governance.rs (L3017-3059)
```rust
        match transfer.from_treasury() {
            TransferFrom::IcpTreasury => self
                .nns_ledger
                .transfer_funds(
                    transfer.amount_e8s,
                    NNS_DEFAULT_TRANSFER_FEE.get_e8s(),
                    self.sns_treasury_icp_subaccount(),
                    to,
                    transfer.memo.unwrap_or(0),
                )
                .await
                .map(|_| ())
                .map_err(|e| {
                    GovernanceError::new_with_message(
                        ErrorType::External,
                        format!("Error making ICP treasury transfer: {e}"),
                    )
                }),
            TransferFrom::SnsTokenTreasury => {
                let transaction_fee_e8s = self.transaction_fee_e8s_or_panic();

                self.ledger
                    .transfer_funds(
                        transfer.amount_e8s,
                        transaction_fee_e8s,
                        self.sns_treasury_sns_token_subaccount(),
                        to,
                        transfer.memo.unwrap_or(0),
                    )
                    .await
                    .map(|_| ())
                    .map_err(|e| {
                        GovernanceError::new_with_message(
                            ErrorType::External,
                            format!("Error making SNS Token treasury transfer: {e}"),
                        )
                    })
            }
            TransferFrom::Unspecified => Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "Invalid 'from_treasury' in transfer.",
            )),
        }
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L369-391)
```text
message TransferSnsTreasuryFunds {
  // Whether to make the transfer from the NNS ledger (in ICP) or
  // to make the transfer from the SNS ledger (in SNS tokens).
  enum TransferFrom {
    TRANSFER_FROM_UNSPECIFIED = 0;
    TRANSFER_FROM_ICP_TREASURY = 1;
    TRANSFER_FROM_SNS_TOKEN_TREASURY = 2;
  }

  TransferFrom from_treasury = 1;

  // The amount to transfer, in e8s.
  uint64 amount_e8s = 2;

  // An optional memo to use for the transfer.
  optional uint64 memo = 3;

  // The principal to transfer the funds to.
  ic_base_types.pb.v1.PrincipalId to_principal = 4;

  // An (optional) Subaccount of the principal to transfer the funds to.
  optional Subaccount to_subaccount = 5;
}
```

**File:** rs/nns/sns-wasm/src/sns_wasm.rs (L4264-4266)
```rust
        let sent_cycles =
            (SNS_CREATION_FEE - CANISTER_CREATION_CYCLES - INITIAL_CANISTER_CREATION_CYCLES) / 6;

```
