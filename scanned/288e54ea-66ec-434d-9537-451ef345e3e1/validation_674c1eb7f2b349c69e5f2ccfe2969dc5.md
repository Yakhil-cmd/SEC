### Title
SNS Governance Voting Rewards Distributed Without Checking `PreInitializationSwap` Mode - (File: rs/sns/governance/src/governance.rs)

### Summary

The SNS Governance canister's `run_periodic_tasks()` function distributes voting rewards to neurons via `distribute_rewards()` without checking whether the canister is in `PreInitializationSwap` mode. Because `MakeProposal` and `RegisterVote` are explicitly permitted in `PreInitializationSwap` mode, developer neurons can submit and vote on proposals, causing the periodic reward distribution to accrue maturity to those neurons during the swap period — bypassing the intent of the mode restriction.

### Finding Description

SNS Governance defines a `PreInitializationSwap` mode (proto field `mode = 19`) intended to restrict operations during the initial decentralization swap to preserve its integrity. [1](#0-0) 

The mode enforcement is applied to user-initiated `manage_neuron` commands and proposal actions via `allows_manage_neuron_command_or_err()` and `allows_proposal_action_or_err()`. Critically, `MakeProposal` and `RegisterVote` are **explicitly allowed** in `PreInitializationSwap` mode: [2](#0-1) 

However, the periodic reward distribution path in `run_periodic_tasks()` contains no mode check: [3](#0-2) 

The `should_distribute_rewards()` function only checks whether `voting_rewards_parameters` is set and whether enough time has elapsed since the last reward event — it never inspects `self.mode()`: [4](#0-3) 

Similarly, `distribute_rewards()` itself performs no mode check before incrementing neuron `maturity_e8s_equivalent`: [5](#0-4) [6](#0-5) 

The test `test_disallow_enabling_voting_rewards_while_in_pre_initialization_swap` confirms the design intent that voting rewards should not be activatable during `PreInitializationSwap` mode via a `ManageNervousSystemParameters` proposal: [7](#0-6) 

Yet if `voting_rewards_parameters` is already set in the initial `NervousSystemParameters` at genesis (which is the standard SNS deployment pattern), rewards will be distributed by the periodic task regardless of mode.

### Impact Explanation

Developer neurons (created at SNS genesis and present during `PreInitializationSwap`) can submit `Motion` proposals and vote on them — both operations are explicitly permitted. When the reward round elapses, `run_periodic_tasks()` calls `distribute_rewards()`, which accrues maturity to those developer neurons proportional to their exercised voting power. After the swap completes and the mode transitions to `Normal`, these neurons can disburse the accumulated maturity. Swap participants, who only receive neurons after finalization, cannot earn any rewards during the swap period. This gives developer neurons an unfair maturity advantage over all future swap participants, distorting the economic fairness of the initial token distribution.

### Likelihood Explanation

The condition is met whenever an SNS is initialized with `voting_rewards_parameters` set (the standard configuration), the swap period spans at least one reward round (typically one day), and developer neurons submit at least one allowed proposal type (e.g., `Motion`). All three conditions are routinely satisfied in production SNS deployments. The entry path requires only an unprivileged ingress call to `manage_neuron` with a `MakeProposal` or `RegisterVote` command from a developer neuron controller — no special privileges are needed.

### Recommendation

Add a mode check at the top of `run_periodic_tasks()` (or inside `should_distribute_rewards()`) to return early and skip reward distribution when `self.mode() == governance::Mode::PreInitializationSwap`. This mirrors the fix applied in the referenced EVM report, which added an emergency-state guard directly inside the restricted function.

```rust
fn should_distribute_rewards(&self) -> bool {
    // Do not distribute rewards during PreInitializationSwap mode.
    if self.mode() == governance::Mode::PreInitializationSwap {
        return false;
    }
    // ... existing logic
}
```

### Proof of Concept

1. An SNS is deployed with `voting_rewards_parameters` set (e.g., `round_duration_seconds = 86400`) and enters `PreInitializationSwap` mode.
2. A developer neuron controller calls `manage_neuron` with `Command::MakeProposal(Motion {...})`. This succeeds because `MakeProposal` is allowed in `PreInitializationSwap` mode per `manage_neuron_command_is_allowed_in_pre_initialization_swap_or_err`.
3. The developer neuron (and any following neurons) vote via `Command::RegisterVote`, also permitted.
4. After `round_duration_seconds` elapses, `run_periodic_tasks()` fires. `should_distribute_rewards()` returns `true` (mode is not checked). `distribute_rewards()` is called.
5. The settled proposal's ballots are iterated; the developer neuron's `maturity_e8s_equivalent` is incremented proportional to its voting power.
6. After the swap finalizes and mode becomes `Normal`, the developer neuron controller calls `manage_neuron` with `Command::DisburseMaturity`, extracting the maturity accrued during the swap period — an advantage unavailable to swap participants. [8](#0-7) [9](#0-8)

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

**File:** rs/sns/governance/src/types.rs (L182-211)
```rust
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

**File:** rs/sns/governance/src/governance.rs (L5471-5534)
```rust
    /// Runs periodic tasks that are not directly triggered by user input.
    pub async fn run_periodic_tasks(&mut self) {
        use ic_cdk::println;

        self.process_proposals();

        // None of the upgrade-related tasks should interleave with one another or themselves, so we acquire a global
        // lock for the duration of their execution. This will return `false` if the lock has already been acquired less
        // than 10 minutes ago by a previous invocation of `run_periodic_tasks`, in which case we skip the
        // upgrade-related tasks.
        if self.acquire_upgrade_periodic_task_lock() {
            // We only want to check the upgrade status if we are currently executing an upgrade.
            if self.should_check_upgrade_status() {
                self.check_upgrade_status().await;
            }

            if self.should_refresh_cached_upgrade_steps() {
                match self.try_temporarily_lock_refresh_cached_upgrade_steps() {
                    Err(err) => {
                        log!(ERROR, "{}", err);
                    }
                    Ok(deployed_version) => {
                        self.refresh_cached_upgrade_steps(deployed_version).await;
                    }
                }
            }

            self.initiate_upgrade_if_sns_behind_target_version().await;

            self.release_upgrade_periodic_task_lock();
        }

        let should_distribute_rewards = self.should_distribute_rewards();

        // Getting the total governance token supply from the ledger is expensive enough
        // that we don't want to do it on every call to `run_periodic_tasks`. So
        // we only fetch it when it's needed, which is when rewards should be
        // distributed
        if should_distribute_rewards {
            match self.ledger.total_supply().await {
                Ok(supply) => {
                    // Distribute rewards
                    self.distribute_rewards(supply);
                }
                Err(e) => log!(
                    ERROR,
                    "Error when getting total governance token supply: {}",
                    GovernanceError::from(e)
                ),
            }
        }

        if self.should_update_maturity_modulation() {
            self.update_maturity_modulation().await;
        }

        self.maybe_finalize_disburse_maturity().await;

        self.maybe_move_staked_maturity();

        self.compute_cached_metrics().await;

        self.maybe_gc();
    }
```

**File:** rs/sns/governance/src/governance.rs (L5725-5753)
```rust
    fn should_distribute_rewards(&self) -> bool {
        let now = self.env.now();

        let voting_rewards_parameters = match &self
            .nervous_system_parameters_or_panic()
            .voting_rewards_parameters
        {
            None => return false,
            Some(ok) => ok,
        };
        let seconds_since_last_reward_event = now.saturating_sub(
            self.latest_reward_event()
                .end_timestamp_seconds
                .unwrap_or_default(),
        );

        let round_duration_seconds = match voting_rewards_parameters.round_duration_seconds {
            Some(s) => s,
            None => {
                log!(
                    ERROR,
                    "round_duration_seconds unset:\n{:#?}",
                    voting_rewards_parameters,
                );
                return false;
            }
        };

        seconds_since_last_reward_event > round_duration_seconds
```

**File:** rs/sns/governance/src/governance.rs (L5763-5780)
```rust
    fn distribute_rewards(&mut self, supply: Tokens) {
        log!(INFO, "distribute_rewards. Supply: {:?}", supply);
        let now = self.env.now();

        // VotingRewardsParameters should always be set,
        // but we check and return early just in case.
        let voting_rewards_parameters = match &self
            .nervous_system_parameters_or_panic()
            .voting_rewards_parameters
        {
            Some(voting_rewards_parameters) => voting_rewards_parameters,
            None => {
                log!(
                    ERROR,
                    "distribute_rewards called even though \
                     voting_rewards_parameters not set.",
                );
                return;
```

**File:** rs/sns/governance/src/governance.rs (L5987-5996)
```rust
                // If the neuron has auto-stake-maturity on, add the new maturity to the
                // staked maturity, otherwise add it to the un-staked maturity.
                if neuron.auto_stake_maturity.unwrap_or(false) {
                    neuron.staked_maturity_e8s_equivalent = Some(
                        neuron.staked_maturity_e8s_equivalent.unwrap_or(0) + neuron_reward_e8s,
                    );
                } else {
                    neuron.maturity_e8s_equivalent += neuron_reward_e8s;
                }
                distributed_e8s_equivalent += neuron_reward_e8s;
```

**File:** rs/sns/governance/src/governance/assorted_governance_tests.rs (L815-871)
```rust
#[tokio::test]
async fn test_disallow_enabling_voting_rewards_while_in_pre_initialization_swap() {
    // Step 1: Prepare the world, i.e. Governance.

    let governance_canister_id = canister_test_id(501);

    let mut env = NativeEnvironment::default();
    env.local_canister_id = Some(governance_canister_id);
    let mut governance = Governance::new(
        GovernanceProto {
            neurons: btreemap! {
                A_NEURON_ID.to_string() => A_NEURON.clone(),
            },
            mode: governance::Mode::PreInitializationSwap as i32,

            ..basic_governance_proto()
        }
        .try_into()
        .unwrap(),
        Box::new(NativeEnvironment::new(Some(CanisterId::from_u64(350519)))),
        Box::new(DoNothingLedger {}),
        Box::new(DoNothingLedger {}),
        Box::new(FakeCmc::new()),
    );

    // Step 2: Run code under test.
    let result = governance
        .make_proposal(
            &A_NEURON_ID,
            &A_NEURON_PRINCIPAL_ID,
            &Proposal {
                action: Some(Action::ManageNervousSystemParameters(
                    NervousSystemParameters {
                        // The operative data is here. Foils make_proposal.
                        voting_rewards_parameters: Some(BASE_VOTING_REWARDS_PARAMETERS),
                        ..Default::default()
                    },
                )),
                ..Default::default()
            },
        )
        .await;

    // Step 3: Inspect result(s).
    let err = match result {
        Ok(ok) => panic!("Proposal should have been rejected: {ok:#?}"),
        Err(err) => err,
    };

    let err = err.error_message.to_lowercase();
    assert!(err.contains("manage nervous system parameters"), "{err:#?}");
    assert!(err.contains("not allowed"), "{err:#?}");
    assert!(
        err.contains("in preinitializationswap (2) mode"),
        "{err:#?}"
    );
}
```
