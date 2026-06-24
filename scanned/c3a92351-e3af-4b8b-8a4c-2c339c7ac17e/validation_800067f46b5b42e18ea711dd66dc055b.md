### Title
SNS Governance Remains Permanently in `PreInitializationSwap` Mode After Aborted Swap, Locking Developer Neuron Holders Out of Their Tokens - (File: `rs/sns/swap/src/swap.rs`)

### Summary
When an SNS token swap is aborted (fails to meet minimum participation), `finalize_inner` returns early without calling `set_sns_governance_to_normal_mode`. This leaves SNS Governance permanently in `PreInitializationSwap` mode, blocking developer neuron holders from disbursing, splitting, or configuring their neurons — permanently locking their staked tokens.

### Finding Description
The SNS Governance canister initializes in `Mode::PreInitializationSwap`, which blocks a broad set of neuron management commands including `Disburse`, `Split`, `MergeMaturity`, `DisburseMaturity`, and `Configure` (start/stop dissolving). [1](#0-0) 

The mode is only supposed to be lifted to `Normal` after a successful swap finalization. In `finalize_inner`, when the swap is aborted (`should_restore_dapp_control()` returns `true`), the function restores dapp controllers and **returns early**, never reaching the `set_sns_governance_to_normal_mode` call: [2](#0-1) 

The `set_sns_governance_to_normal_mode` call only executes on the committed (successful) path: [3](#0-2) 

`should_restore_dapp_control` is simply: [4](#0-3) 

Furthermore, `set_mode` on the governance canister can only be called by the swap canister and only to set `Normal` mode — there is no governance proposal path to exit `PreInitializationSwap` mode: [5](#0-4) 

This behavior is confirmed by integration tests that explicitly assert governance stays in `PreInitializationSwap` after an abort, and that `start_dissolving_neuron` and `ManageNervousSystemParameters` proposals fail: [6](#0-5) [7](#0-6) 

The system-level test helper also asserts this post-abort state: [8](#0-7) 

### Impact Explanation
Developer neuron holders who received SNS tokens at genesis (via `DeveloperDistribution`) have their tokens permanently locked in neurons after a failed swap. They cannot:
- `Disburse` tokens out of neurons
- `Split` neurons
- `DisburseMaturity` or `MergeMaturity`
- `Configure` neurons (start/stop dissolving, change dissolve delay) [9](#0-8) 

The only escape is a canister upgrade (requiring NNS governance action), which is not a user-accessible remedy. This is a direct analog to the reported vulnerability: a "paused" state triggered by a protocol-level adverse event (swap failure) that prevents legitimate users from accessing their funds.

### Likelihood Explanation
Any SNS swap that fails to meet minimum participation requirements automatically transitions to `Lifecycle::Aborted` and triggers `finalize_inner` via the auto-finalization heartbeat. This is a normal protocol event — not an edge case. Any SNS project whose swap fails (which has happened on mainnet) permanently locks developer neuron holders out of their tokens without any user-accessible remedy. [10](#0-9) 

### Recommendation
In `finalize_inner`, when the swap is aborted, call `set_sns_governance_to_normal_mode` before returning, so that developer neuron holders can access their tokens after a failed swap. The `PreInitializationSwap` mode restriction is only meaningful during the swap itself — after abort, there is no integrity reason to keep it active. Alternatively, introduce a dedicated `Aborted` mode that lifts neuron management restrictions while still preventing harmful governance actions.

### Proof of Concept
1. An SNS is deployed; SNS Governance starts in `PreInitializationSwap` mode. Developer neurons are created with tokens.
2. The SNS swap opens but fails to reach `min_participants` or `min_icp_e8s` before the deadline.
3. `try_abort` is called (automatically on heartbeat), setting `lifecycle = Aborted`.
4. `finalize` → `finalize_inner` is called. `should_restore_dapp_control()` returns `true` (lifecycle is Aborted). The function restores dapp controllers and returns at line 1583 — **without calling `set_sns_governance_to_normal_mode`**.
5. A developer neuron holder calls `manage_neuron` with `Command::Disburse` or `Command::Configure(StartDissolving)`.
6. `allows_manage_neuron_command_or_err` is called with `Mode::PreInitializationSwap`, returns `Err(PreconditionFailed, "Because governance is currently in PreInitializationSwap mode, manage_neuron commands of this type are not allowed")`.
7. The developer's tokens remain permanently locked. There is no user-callable method to exit `PreInitializationSwap` mode. [11](#0-10) [12](#0-11)

### Citations

**File:** rs/sns/governance/src/types.rs (L163-211)
```rust
impl governance::Mode {
    pub fn allows_manage_neuron_command_or_err(
        &self,
        command: &manage_neuron::Command,
        caller_is_swap_canister: bool,
    ) -> Result<(), GovernanceError> {
        use governance::Mode;
        match self {
            Mode::Unspecified => panic!("Governance's mode is not specified."),
            Mode::Normal => Ok(()),
            Mode::PreInitializationSwap => {
                Self::manage_neuron_command_is_allowed_in_pre_initialization_swap_or_err(
                    command,
                    caller_is_swap_canister,
                )
            }
        }
    }

    fn manage_neuron_command_is_allowed_in_pre_initialization_swap_or_err(
        command: &manage_neuron::Command,
        caller_is_swap_canister: bool,
    ) -> Result<(), GovernanceError> {
        use manage_neuron::Command as C;
        let ok = match command {
            C::Follow(_)
            | C::MakeProposal(_)
            | C::RegisterVote(_)
            | C::AddNeuronPermissions(_)
            | C::RemoveNeuronPermissions(_) => true,

            C::ClaimOrRefresh(_) => caller_is_swap_canister,

            _ => false,
        };

        if ok {
            return Ok(());
        }

        Err(GovernanceError::new_with_message(
            ErrorType::PreconditionFailed,
            format!(
                "Because governance is currently in PreInitializationSwap mode, \
                 manage_neuron commands of this type are not allowed \
                 (caller_is_swap_canister={caller_is_swap_canister}). command: {command:#?}",
            ),
        ))
    }
```

**File:** rs/sns/swap/src/swap.rs (L1348-1350)
```rust
    pub fn should_restore_dapp_control(&self) -> bool {
        self.lifecycle() == Lifecycle::Aborted
    }
```

**File:** rs/sns/swap/src/swap.rs (L1544-1584)
```rust
    pub async fn finalize_inner(
        &mut self,
        now_fn: fn(bool) -> u64,
        environment: &mut impl CanisterEnvironment,
    ) -> FinalizeSwapResponse {
        let mut finalize_swap_response = FinalizeSwapResponse::default();

        if let Err(e) = self.can_finalize() {
            finalize_swap_response.set_error_message(e);
            return finalize_swap_response;
        }

        // Transfer the ICP tokens from the Swap canister.
        finalize_swap_response
            .set_sweep_icp_result(self.sweep_icp(now_fn, environment.icp_ledger()).await);
        if finalize_swap_response.has_error_message() {
            return finalize_swap_response;
        }

        // Settle the Neurons' Fund participation in the token swap.
        finalize_swap_response.set_settle_neurons_fund_participation_result(
            self.settle_neurons_fund_participation(environment.nns_governance_mut())
                .await,
        );
        if finalize_swap_response.has_error_message() {
            return finalize_swap_response;
        }

        if self.should_restore_dapp_control() {
            // Restore controllers of dapp canisters to their original
            // owners (i.e. self.init.fallback_controller_principal_ids).
            finalize_swap_response.set_set_dapp_controllers_result(
                self.restore_dapp_controllers_for_finalize(environment.sns_root_mut())
                    .await,
            );

            // In the case of returning control of the dapp(s) to the fallback
            // controllers, finalize() need not do any more work, so always return
            // and end execution.
            return finalize_swap_response;
        }
```

**File:** rs/sns/swap/src/swap.rs (L1610-1612)
```rust
        finalize_swap_response.set_set_mode_call_result(
            Self::set_sns_governance_to_normal_mode(environment.sns_governance_mut()).await,
        );
```

**File:** rs/sns/swap/src/swap.rs (L2906-2914)
```rust
    pub fn can_abort(&self, now_seconds: u64) -> bool {
        if self.lifecycle() != Lifecycle::Open {
            return false;
        }

        // if the swap is due or the ICP target is reached without sufficient participation, we can abort
        (self.swap_due(now_seconds) || self.icp_target_progress().is_reached_or_exceeded())
            && !self.sufficient_participation()
    }
```

**File:** rs/sns/governance/src/governance.rs (L785-800)
```rust
    pub fn set_mode(&mut self, mode: i32, caller: PrincipalId) {
        let mode =
            governance::Mode::try_from(mode).unwrap_or_else(|_| panic!("Unknown mode: {mode}"));

        if !self.is_swap_canister(caller) {
            panic!("Caller must be the swap canister.");
        }

        // As of Aug, 2022, the only use-case we have for set_mode is to enter
        // Normal mode (from PreInitializationSwap). Therefore, this is here
        // just to make sure we do not proceed with unexpected operations.
        if mode != governance::Mode::Normal {
            panic!("Entering {mode:?} mode is not allowed.");
        }

        self.proto.mode = mode as i32;
```

**File:** rs/nervous_system/integration_tests/tests/sns_lifecycle.rs (L1118-1138)
```rust
    if swap_finalization_status == SwapFinalizationStatus::Aborted {
        assert_eq!(
            sns.governance
                .get_mode(&pocket_ic)
                .await
                .unwrap()
                .mode
                .unwrap(),
            sns_pb::governance::Mode::PreInitializationSwap as i32,
        );
    } else {
        assert_eq!(
            sns.governance
                .get_mode(&pocket_ic)
                .await
                .unwrap()
                .mode
                .unwrap(),
            sns_pb::governance::Mode::Normal as i32
        );
    }
```

**File:** rs/nervous_system/integration_tests/tests/sns_lifecycle.rs (L1268-1289)
```rust
        if swap_finalization_status == SwapFinalizationStatus::Aborted {
            match start_dissolving_response.command {
                Some(sns_pb::manage_neuron_response::Command::Error(error)) => {
                    let sns_pb::GovernanceError {
                        error_type,
                        error_message,
                    } = &error;
                    use sns_pb::governance_error::ErrorType;
                    assert_eq!(
                        ErrorType::try_from(*error_type).unwrap(),
                        ErrorType::PreconditionFailed,
                        "{error:#?}"
                    );
                    assert!(
                        error_message.contains("PreInitializationSwap"),
                        "{error:#?}"
                    );
                }
                response => {
                    panic!("{response:#?}");
                }
            };
```

**File:** rs/tests/nns/sns/lib/src/swap_finalization.rs (L123-125)
```rust
    sns_client
        .assert_state(&env, Lifecycle::Aborted, Mode::PreInitializationSwap)
        .await;
```
