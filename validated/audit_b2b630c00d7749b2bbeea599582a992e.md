### Title
Voting Rewards Distributed During `PreInitializationSwap` Mode Without Mode Guard - (`rs/sns/governance/src/governance.rs`)

### Summary

SNS governance has a `PreInitializationSwap` mode that restricts many operations to protect the integrity of the initial token swap. However, `run_periodic_tasks` distributes voting rewards unconditionally during this mode, giving initial neurons (founders/team) an exclusive advantage over swap participants who have not yet received their neurons.

### Finding Description

SNS governance defines a `Mode` enum with `Normal` and `PreInitializationSwap` variants. The `PreInitializationSwap` mode is designed to restrict operations during the token swap period. It blocks `manage_neuron` commands such as `Disburse`, `Split`, `MergeMaturity`, `DisburseMaturity`, and `Configure`, and blocks proposal actions such as `ManageNervousSystemParameters`, `TransferSnsTreasuryFunds`, `MintSnsTokens`, and others.

However, the `run_periodic_tasks` function in `rs/sns/governance/src/governance.rs` does not check `self.proto.mode` before calling `distribute_rewards`. Specifically:

```rust
// rs/sns/governance/src/governance.rs ~line 5503
let should_distribute_rewards = self.should_distribute_rewards();
if should_distribute_rewards {
    match self.ledger.total_supply().await {
        Ok(supply) => {
            self.distribute_rewards(supply);
        }
        ...
    }
}
```

The `should_distribute_rewards` function only checks whether enough time has elapsed since the last reward event — it never inspects `self.proto.mode`:

```rust
fn should_distribute_rewards(&self) -> bool {
    // Only checks timing, never checks mode
    seconds_since_last_reward_event > round_duration_seconds
}
```

Similarly, `distribute_rewards` itself contains no mode guard. During `PreInitializationSwap`, voting (`RegisterVote`, `MakeProposal`) is explicitly allowed, so initial neurons can vote and earn rewards. Swap participants, who have not yet received their neurons, cannot vote and thus cannot earn any rewards during this period.

### Impact Explanation

If an SNS has `voting_rewards_parameters` configured before entering `PreInitializationSwap` mode, voting rewards continue to be distributed automatically via the canister timer during the entire swap period (which can last days or weeks). All rewards during this window flow exclusively to the initial neurons (founders/team), since swap participants have no neurons yet. This is an unfair inflation of maturity for insiders during the swap period, directly analogous to the GoGoPool finding where rewards accrued during a paused state.

The test `test_disallow_enabling_voting_rewards_while_in_pre_initialization_swap` shows the intent to restrict reward-related changes during the swap, but it only blocks enabling rewards via a governance proposal — it does not stop already-configured rewards from being distributed.

### Likelihood Explanation

This occurs automatically on every SNS that:
1. Has `voting_rewards_parameters` configured (common for SNSes that want to incentivize governance participation), and
2. Enters `PreInitializationSwap` mode (every SNS that goes through the standard launch flow).

No attacker action is required. The canister timer fires `run_periodic_tasks` automatically. The entry path is the IC timer subsystem, which is unprivileged and automatic.

### Recommendation

Add a mode check in `should_distribute_rewards` (or at the call site in `run_periodic_tasks`) to skip reward distribution when the governance is in `PreInitializationSwap` mode:

```rust
fn should_distribute_rewards(&self) -> bool {
    // Do not distribute rewards during PreInitializationSwap
    if self.proto.mode() == governance::Mode::PreInitializationSwap {
        return false;
    }
    // ... existing timing check
}
```

This mirrors the pattern already used for `allows_manage_neuron_command_or_err` and `allows_proposal_action_or_err`, which both gate on `self.proto.mode`.

### Proof of Concept

1. An SNS is created with `voting_rewards_parameters` set (e.g., `round_duration_seconds = 86400`, non-zero reward rate).
2. The SNS enters `PreInitializationSwap` mode when the swap proposal is adopted.
3. The canister timer fires `run_periodic_tasks` every round.
4. `should_distribute_rewards` returns `true` after one round duration elapses.
5. `distribute_rewards` is called, computing a reward purse from the total token supply and distributing maturity to all neurons that voted.
6. Only initial neurons (founders/team) can vote during this period; swap participants have no neurons.
7. After the swap completes and participants receive their neurons, the initial neurons have already accumulated extra maturity that swap participants could not earn.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** rs/sns/governance/src/governance.rs (L5503-5521)
```rust
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

**File:** rs/sns/governance/src/gen/ic_sns_governance.pb.v1.rs (L2407-2415)
```rust
    pub enum Mode {
        /// This forces people to explicitly populate the mode field.
        Unspecified = 0,
        /// All operations are allowed.
        Normal = 1,
        /// In this mode, various operations are not allowed in order to ensure the
        /// integrity of the initial token swap.
        PreInitializationSwap = 2,
    }
```
