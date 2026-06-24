### Title
Unbounded Neuron Iteration in `compute_ballots_for_new_proposal` Exhausts Instruction Limit, Permanently Freezing SNS Proposal Submission - (File: `rs/sns/governance/src/governance.rs`)

---

### Summary

The SNS governance canister's `compute_ballots_for_new_proposal` function iterates over every neuron in `self.proto.neurons` in a single synchronous loop with no instruction-limit guard. Because any user can create neurons by staking SNS tokens, a sufficiently large neuron population causes every `make_proposal` call to exceed the IC instruction limit, permanently preventing new proposals from being submitted and freezing SNS governance.

---

### Finding Description

`compute_ballots_for_new_proposal` is called unconditionally from `make_proposal` before any state mutation occurs: [1](#0-0) 

The function iterates over the entire `self.proto.neurons` map without any instruction-limit check or ability to pause and resume: [2](#0-1) 

There is no call to `is_message_over_threshold`, `noop_self_call_if_over_instructions`, or any equivalent guard inside this loop. The loop is purely synchronous and must complete within a single message execution.

By contrast, the NNS governance canister explicitly guards its analogous long-running operations. The reward distribution path uses `is_message_over_threshold(DISTRIBUTION_MESSAGE_LIMIT)`: [3](#0-2) 

The voting cascade path uses `noop_self_call_if_over_instructions` with both soft and hard limits: [4](#0-3) 

The SNS governance `compute_ballots_for_new_proposal` has no equivalent protection.

---

### Impact Explanation

When the number of eligible neurons in an SNS grows large enough that iterating over all of them (computing `dissolve_delay_seconds` and `voting_power` per neuron) exceeds the IC per-message instruction limit (~40 billion instructions on application subnets), every call to `make_proposal` will be rejected with `CanisterInstructionLimitExceeded`. Since `compute_ballots_for_new_proposal` is called before any state is mutated, the failure is clean but permanent: no new proposals can ever be submitted again. This freezes SNS governance entirely — no upgrades, no parameter changes, no treasury actions.

---

### Likelihood Explanation

Any principal can stake SNS tokens and call `claim_or_refresh_neuron` to create neurons. This is a normal, permissionless user action. Organic SNS growth (many participants staking) can reach the threshold without any deliberate attack. A targeted attacker who acquires SNS tokens can accelerate this by creating many neurons across many principals. The NNS governance bench test already projects that iterating over `MAX_NUMBER_OF_NEURONS` neurons in a single ballot computation costs up to 25 billion instructions: [5](#0-4) 

SNS governance performs the same per-neuron work in `compute_ballots_for_new_proposal` with no chunking, making the instruction budget the binding constraint.

---

### Recommendation

Apply the same instruction-limit-aware chunking pattern already used in NNS governance. Specifically:

1. Convert `compute_ballots_for_new_proposal` to use `is_message_over_threshold` inside the neuron loop, storing partial results in stable state and resuming across messages (as done in `distribute_pending_rewards`).
2. Alternatively, pre-compute and cache the electoral roll (voting power snapshot) on a timer, similar to how NNS governance maintains `VOTING_POWER_SNAPSHOTS`, so that `make_proposal` reads from a pre-built snapshot rather than iterating all neurons inline.
3. At minimum, enforce a hard cap on the number of neurons eligible to vote per proposal, with a documented and enforced `MAX_NEURONS_FOR_PROPOSAL` constant, and reject `make_proposal` with a clear error if the cap would be exceeded.

---

### Proof of Concept

1. Deploy an SNS with default parameters.
2. Have many principals each stake SNS tokens and call `claim_or_refresh_neuron` with a dissolve delay above `neuron_minimum_dissolve_delay_to_vote_seconds`, creating a large number of eligible neurons.
3. Call `make_proposal` from any neuron with sufficient stake.
4. Observe that `make_proposal` calls `compute_ballots_for_new_proposal` at: [1](#0-0) 

which enters the unbounded loop at: [2](#0-1) 

5. Once the neuron count is large enough, the call is rejected with `CanisterInstructionLimitExceeded`. All subsequent `make_proposal` calls fail identically. SNS governance is permanently frozen with no recovery path short of a canister upgrade — which itself requires a proposal, creating a deadlock.

### Citations

**File:** rs/sns/governance/src/governance.rs (L3557-3559)
```rust
        let (_, electoral_roll) = self
            .compute_ballots_for_new_proposal()
            .map_err(|err| GovernanceError::new_with_message(ErrorType::PreconditionFailed, err))?;
```

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

**File:** rs/nns/governance/src/reward/distribution.rs (L42-51)
```rust
    pub fn distribute_pending_rewards(&mut self) -> bool {
        let is_over_instructions_limit = || is_message_over_threshold(DISTRIBUTION_MESSAGE_LIMIT);
        with_rewards_distribution_state_machine_mut(|rewards_distribution_state_machine| {
            rewards_distribution_state_machine.with_next_distribution(|(_, distribution)| {
                distribution
                    .continue_processing(&mut self.neuron_store, is_over_instructions_limit);
            });
            // Work left?
            !rewards_distribution_state_machine.distributions.is_empty()
        })
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

**File:** rs/nns/governance/src/governance/benches.rs (L473-478)
```rust
    check_projected_instructions(
        bench_result,
        num_neurons,
        MAX_NUMBER_OF_NEURONS as u64,
        25_000_000_000,
    )
```
