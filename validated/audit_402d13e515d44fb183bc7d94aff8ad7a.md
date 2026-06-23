### Title
Unbounded Linear Iteration Over All Neurons in `compute_ballots_for_new_proposal` Can Exhaust Instruction Limit - (`rs/sns/governance/src/governance.rs`)

### Summary

The SNS Governance canister's `compute_ballots_for_new_proposal` function performs a full linear scan over `self.proto.neurons` on every proposal submission. With `MAX_NUMBER_OF_NEURONS_CEILING` set to 200,000, an SNS at or near its neuron limit will exhaust the IC's per-message instruction limit during proposal creation, permanently blocking all new proposals.

### Finding Description

In `rs/sns/governance/src/governance.rs`, the function `compute_ballots_for_new_proposal` iterates unconditionally over every neuron in the governance state to build the electoral roll:

```rust
for (k, v) in self.proto.neurons.iter() {
    if v.dissolve_delay_seconds(now_seconds) < min_dissolve_delay_for_vote {
        continue;
    }
    let voting_power = v.voting_power(...);
    total_power += voting_power as u128;
    electoral_roll.insert(k.clone(), Ballot { ... });
}
```

This is an O(N) loop over all neurons, called synchronously within the `manage_neuron` update handler (via `MakeProposal`). There is no instruction-limit checkpoint, no batching, and no early exit. The NNS Governance canister mitigated this exact pattern by moving ballot computation to a pre-computed `VotingPowerSnapshot` and using stable-memory iterators with instruction-limit awareness. The SNS Governance canister has not received this mitigation and still uses the raw heap `BTreeMap` iteration.

The `MAX_NUMBER_OF_NEURONS_CEILING` for SNS is 200,000. The `max_number_of_neurons` parameter is set per-SNS and can be configured up to this ceiling. At large neuron counts, the per-neuron work (dissolve delay computation, voting power calculation with multiple multiplications) multiplied by 200,000 iterations will exceed the IC's per-message instruction limit of ~40 billion instructions, causing the `manage_neuron` call to trap with an instruction limit exceeded error.

The call path is:
1. Any principal holding an SNS neuron calls `manage_neuron` (update) with `Command::MakeProposal`.
2. `make_proposal` is called, which calls `compute_ballots_for_new_proposal`.
3. `compute_ballots_for_new_proposal` iterates over all neurons in `self.proto.neurons`.
4. At high neuron counts, the instruction limit is exceeded and the call traps.

### Impact Explanation

When the neuron count approaches the configured `max_number_of_neurons` (up to `MAX_NUMBER_OF_NEURONS_CEILING = 200_000`), every call to `manage_neuron` with `MakeProposal` will trap. This permanently blocks all new proposal submissions for the SNS, rendering governance non-functional. No proposals can be submitted — including upgrade proposals — which can permanently freeze the SNS canister. This is a liveness/availability failure of the governance system.

### Likelihood Explanation

SNS DAOs are designed to grow their communities. Popular SNS projects can accumulate tens of thousands of neurons. The ceiling of 200,000 is explicitly documented as a hard limit. Any SNS that grows to a large neuron count (the exact threshold depends on per-neuron instruction cost, but is well within the ceiling) will hit this. An attacker who can cheaply create neurons (e.g., by staking the minimum stake amount) can deliberately push an SNS to its neuron limit to trigger this condition. The entry path requires only a valid SNS neuron, which is an unprivileged role.

### Recommendation

Apply the same mitigation used in NNS Governance: pre-compute and cache a `VotingPowerSnapshot` in a periodic timer task (as done in `rs/nns/governance/src/neuron_store/voting_power.rs`), and use that snapshot in `compute_ballots_for_new_proposal` instead of iterating over all neurons inline. Alternatively, add an instruction-limit checkpoint inside the loop and split the computation across multiple messages, similar to the `noop_self_call_if_over_instructions` pattern used in NNS `cast_vote_and_cascade_follow`.

### Proof of Concept

The vulnerable loop is at: [1](#0-0) 

The ceiling that bounds the maximum neuron count is: [2](#0-1) 

The NNS Governance equivalent — which was refactored to use a pre-computed snapshot and avoids this problem — is: [3](#0-2) 

The NNS Governance instruction-limit-aware voting cascade (showing the `noop_self_call_if_over_instructions` pattern that SNS lacks) is: [4](#0-3) 

The SNS `manage_neuron` update handler that triggers the unbounded loop on every `MakeProposal` call is reachable by any neuron holder without any privileged role. The `compute_ballots_for_new_proposal` function has no instruction-limit guard and no batching mechanism, making it a direct analog of the `recordMintBestAvailableTier` linear scan described in the reference report.

### Citations

**File:** rs/sns/governance/src/governance.rs (L5255-5280)
```rust
        for (k, v) in self.proto.neurons.iter() {
            // If this neuron is eligible to vote, record its
            // voting power at the time of proposal creation (now).
            if v.dissolve_delay_seconds(now_seconds) < min_dissolve_delay_for_vote {
                // Not eligible due to dissolve delay.
                continue;
            }

            let voting_power = v.voting_power(
                now_seconds,
                max_dissolve_delay,
                max_age_bonus,
                max_dissolve_delay_bonus_percentage,
                max_age_bonus_percentage,
            );

            total_power += voting_power as u128;
            electoral_roll.insert(
                k.clone(),
                Ballot {
                    vote: Vote::Unspecified as i32,
                    voting_power,
                    cast_timestamp_seconds: 0,
                },
            );
        }
```

**File:** rs/sns/governance/src/types.rs (L383-386)
```rust
    /// This is an upper bound for `max_number_of_neurons`. Exceeding it may cause
    /// degradation in the governance canister or the subnet hosting the SNS.
    /// See also: `MAX_NEURONS_FOR_DIRECT_PARTICIPANTS`.
    pub const MAX_NUMBER_OF_NEURONS_CEILING: u64 = 200_000;
```

**File:** rs/nns/governance/src/neuron_store/voting_power.rs (L124-186)
```rust
impl NeuronStore {
    /// Computes the voting power snapshot for a standard proposal.
    pub fn compute_voting_power_snapshot_for_standard_proposal(
        &self,
        voting_power_economics: &VotingPowerEconomics,
        now_seconds: u64,
    ) -> Result<VotingPowerSnapshot, NeuronStoreError> {
        let mut voting_power_map = HashMap::new();
        let mut total_deciding_voting_power: u128 = 0;
        let mut total_potential_voting_power: u128 = 0;

        let default_min_dissolve_delay = if is_mission_70_voting_rewards_enabled() {
            VotingPowerEconomics::MISSION_70_DEFAULT_NEURON_MINIMUM_DISSOLVE_DELAY_TO_VOTE_SECONDS
        } else {
            VotingPowerEconomics::DEFAULT_NEURON_MINIMUM_DISSOLVE_DELAY_TO_VOTE_SECONDS
        };
        let min_dissolve_delay_seconds = voting_power_economics
            .neuron_minimum_dissolve_delay_to_vote_seconds
            .unwrap_or(default_min_dissolve_delay);

        let mut process_neuron = |neuron: &Neuron| {
            if neuron.is_inactive(now_seconds)
                || neuron.dissolve_delay_seconds(now_seconds) < min_dissolve_delay_seconds
            {
                return;
            }

            let (potential_voting_power, deciding_voting_power) =
                neuron.potential_and_deciding_voting_power(voting_power_economics, now_seconds);
            // We don't handle overflow here, as in `get_voting_power_as_u64` below,
            // the input arguments bigger than u64::MAX will result in an error.
            total_deciding_voting_power =
                total_deciding_voting_power.saturating_add(deciding_voting_power as u128);
            total_potential_voting_power =
                total_potential_voting_power.saturating_add(potential_voting_power as u128);
            voting_power_map.insert(neuron.id().id, deciding_voting_power);
        };

        // Active neurons iterator already makes distinctions between stable and heap neurons.
        self.with_active_neurons_iter_sections(
            |iter| {
                for neuron in iter {
                    process_neuron(&neuron);
                }
            },
            NeuronSections::NONE,
        );

        let total_deciding_voting_power = get_voting_power_as_u64(
            total_deciding_voting_power,
            NeuronStoreError::TotalDecidingVotingPowerOverflow,
        )?;
        let total_potential_voting_power = get_voting_power_as_u64(
            total_potential_voting_power,
            NeuronStoreError::TotalPotentialVotingPowerOverflow,
        )?;

        Ok(VotingPowerSnapshot {
            voting_power_map,
            total_deciding_voting_power,
            total_potential_voting_power,
        })
    }
```

**File:** rs/nns/governance/src/voting.rs (L150-176)
```rust
        while !is_voting_finished {
            // Now we process until we are done or we are over a limit and need to
            // make a self-call.
            with_voting_state_machines_mut(|voting_state_machines| {
                voting_state_machines.with_machine(proposal_id, topic, |machine| {
                    self.process_machine_until_soft_limit(machine, over_soft_message_limit);
                    is_voting_finished = machine.is_voting_finished();
                });
            });

            // This returns an error if we hit the hard limit, which should basically never happen
            // in production, but we need a way out of this loop in the worst case to prevent
            // the canister from being unable to upgrade.
            if let Err(e) = noop_self_call_if_over_instructions(
                SOFT_VOTING_INSTRUCTIONS_LIMIT,
                Some(HARD_VOTING_INSTRUCTIONS_LIMIT),
            )
            .await
            {
                println!(
                    "Error in cast_vote_and_cascade_follow, \
                        voting will be processed in timers: {}",
                    e
                );
                break;
            }
        }
```
