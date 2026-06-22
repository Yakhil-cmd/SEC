### Title
SNS Governance `perform_action` Does Not Gate on `PreInitializationSwap` Mode, Allowing Adopted Proposals to Execute After Mode Transition - (File: rs/sns/governance/src/governance.rs)

### Summary
The SNS governance's mode restriction (`allows_proposal_action_or_err`) is enforced only at proposal *submission* time inside `make_proposal`, not at proposal *execution* time inside `perform_action`. A proposal for `TransferSnsTreasuryFunds` or `MintSnsTokens` that was adopted while the SNS was in `Normal` mode can still execute after governance transitions to `PreInitializationSwap` mode, violating the integrity guarantees that mode is designed to enforce during the token swap.

### Finding Description
SNS Governance defines two modes in `rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto`:

- `MODE_NORMAL = 1` — all operations allowed
- `MODE_PRE_INITIALIZATION_SWAP = 2` — restricted mode during token swap [1](#0-0) 

In `PreInitializationSwap` mode, the following actions are explicitly disallowed via `Mode::functions_disallowed_in_pre_initialization_swap()` in `rs/sns/governance/src/types.rs`:

- `TransferSnsTreasuryFunds`
- `MintSnsTokens`
- `ManageNervousSystemParameters`
- `UpgradeSnsControlledCanister`
- `RegisterDappCanisters`
- `DeregisterDappCanisters` [2](#0-1) 

The mode check is applied via `allows_proposal_action_or_err` only at submission time. The enforcement path is: [3](#0-2) 

However, the execution path — `process_proposal` → `start_proposal_execution` → `perform_action` — does **not** re-check the current mode before dispatching the action: [4](#0-3) 

`perform_action` dispatches directly to the action handler (e.g., `TransferSnsTreasuryFunds`, `MintSnsTokens`) without consulting `self.proto.mode()`: [5](#0-4) 

The mode transition from `Normal` → `PreInitializationSwap` is triggered by the swap canister calling `set_mode` on the governance canister. This is a separate inter-canister message from the proposal execution path. Because `run_periodic_tasks` drives proposal execution asynchronously, a proposal that reached the `Adopted` decision in one round can be executed by `process_proposal` in a subsequent round — after `set_mode` has already flipped the mode.

### Impact Explanation
If a `TransferSnsTreasuryFunds` or `MintSnsTokens` proposal is adopted in `Normal` mode and executes after the mode transitions to `PreInitializationSwap`:

1. **`TransferSnsTreasuryFunds`** can drain SNS treasury tokens (ICP or SNS tokens) to an arbitrary account during the swap window. This reduces the treasury balance that swap participants expect to be stable, and can affect the economic integrity of the swap.
2. **`MintSnsTokens`** can inflate the SNS token supply during the swap, diluting the per-token value received by swap participants and breaking the supply invariant the swap pricing depends on.

Both violate the documented purpose of `PreInitializationSwap` mode: to freeze governance-driven treasury and token operations while the decentralization swap is in progress.

### Likelihood Explanation
The mode transition from `Normal` to `PreInitializationSwap` is triggered by a known, scheduled NNS event (execution of a `CreateServiceNervousSystem` NNS proposal). The timing is observable on-chain. A participant who holds sufficient voting power to adopt a proposal — or who can coordinate with other neurons — can submit a `TransferSnsTreasuryFunds` or `MintSnsTokens` proposal, ensure it reaches `Adopted` state just before the NNS proposal executes and flips the mode, and then allow `run_periodic_tasks` to execute it in the new mode. No privileged key or subnet-majority is required; only SNS governance voting power, which is an externally reachable entry point for any token holder. Likelihood is **low-medium**: the timing window is narrow (one or a few heartbeat intervals), but the event is predictable and the entry path is unprivileged.

### Recommendation
Re-check the current governance mode inside `perform_action` (or `process_proposal`) before dispatching any action, mirroring the check already present in `make_proposal`:

```rust
async fn perform_action(&mut self, proposal_id: u64, action: Action) {
    // Gate on current mode at execution time, not just at submission time.
    let mode = self.proto.mode();
    if let Err(e) = mode.allows_proposal_action_or_err(
        &action,
        &self.disallowed_target_canister_ids(),
        &self.proto.id_to_nervous_system_functions,
    ) {
        self.set_proposal_execution_status(proposal_id, Err(e));
        return;
    }
    // ... existing dispatch logic
}
```

This ensures that even proposals adopted in `Normal` mode cannot execute actions that are forbidden in `PreInitializationSwap` mode.

### Proof of Concept
1. SNS is in `Normal` mode. An SNS token holder submits a `TransferSnsTreasuryFunds` proposal targeting an account they control.
2. The proposal accumulates sufficient votes and transitions to `Adopted` state. `process_proposal` has not yet been called for this proposal (it runs in `run_periodic_tasks`).
3. The NNS executes a `CreateServiceNervousSystem` proposal. As part of finalization, the swap canister calls `set_mode(PreInitializationSwap)` on the SNS governance canister. The mode field is now `MODE_PRE_INITIALIZATION_SWAP`.
4. On the next invocation of `run_periodic_tasks`, `process_proposal` is called for the adopted proposal. It calls `start_proposal_execution`, which spawns `perform_action`.
5. `perform_action` dispatches to the `TransferSnsTreasuryFunds` handler without checking `self.proto.mode()`. The transfer executes, moving SNS treasury funds to the attacker's account during the active swap window.
6. Swap participants now interact with a depleted treasury, violating the economic integrity of the decentralization swap.

### Citations

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L1591-1601)
```text
  enum Mode {
    // This forces people to explicitly populate the mode field.
    MODE_UNSPECIFIED = 0;

    // All operations are allowed.
    MODE_NORMAL = 1;

    // In this mode, various operations are not allowed in order to ensure the
    // integrity of the initial token swap.
    MODE_PRE_INITIALIZATION_SWAP = 2;
  }
```

**File:** rs/sns/governance/src/types.rs (L229-251)
```rust
    pub fn allows_proposal_action_or_err(
        &self,
        action: &Action,
        disallowed_target_canister_ids: &HashSet<CanisterId>,
        id_to_nervous_system_function: &BTreeMap<u64, NervousSystemFunction>,
    ) -> Result<(), GovernanceError> {
        use governance::Mode;
        match self {
            Mode::Normal => Ok(()),

            Mode::PreInitializationSwap => {
                Self::proposal_action_is_allowed_in_pre_initialization_swap_or_err(
                    action,
                    disallowed_target_canister_ids,
                    id_to_nervous_system_function,
                )
            }

            Mode::Unspecified => {
                panic!("Governance's mode is not specified.");
            }
        }
    }
```

**File:** rs/sns/governance/src/types.rs (L253-262)
```rust
    pub fn functions_disallowed_in_pre_initialization_swap() -> Vec<NervousSystemFunction> {
        vec![
            NervousSystemFunction::manage_nervous_system_parameters(),
            NervousSystemFunction::transfer_sns_treasury_funds(),
            NervousSystemFunction::mint_sns_tokens(),
            NervousSystemFunction::upgrade_sns_controlled_canister(),
            NervousSystemFunction::register_dapp_canisters(),
            NervousSystemFunction::deregister_dapp_canisters(),
        ]
    }
```

**File:** rs/sns/governance/src/governance.rs (L2118-2134)
```rust
    fn start_proposal_execution(&mut self, proposal_id: u64, action: Action) {
        // `perform_action` is an async method of &mut self.
        //
        // Starting it and letting it run in the background requires knowing that
        // the `self` reference will last until the future has completed.
        //
        // The compiler cannot know that, but this is actually true:
        //
        // - in unit tests, all futures are immediately ready, because no real async
        //   call is made. In this case, the transmutation to a static ref is abusive,
        //   but it's still ok since the future will immediately resolve.
        //
        // - in prod, "self" is a reference to the GOVERNANCE static variable, which is
        //   initialized only once (in canister_init or canister_post_upgrade)
        let governance: &'static mut Governance = unsafe { std::mem::transmute(self) };
        spawn_in_canister_env(governance.perform_action(proposal_id, action));
    }
```

**File:** rs/sns/governance/src/governance.rs (L2139-2199)
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
```
