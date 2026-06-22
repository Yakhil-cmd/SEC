### Title
Unbounded Synchronous Loop Over All Neurons in `compute_ballots_for_new_proposal` Causes Instruction-Limit Exhaustion Denial of Service - (File: rs/sns/governance/src/governance.rs)

### Summary

The SNS governance canister's `compute_ballots_for_new_proposal` function iterates synchronously over every neuron in `self.proto.neurons` without any instruction-limit check or pagination. With up to 200,000 neurons permitted by `MAX_NUMBER_OF_NEURONS_CEILING`, a single `make_proposal` call can exhaust the IC instruction limit, causing the call to trap and permanently preventing any new proposal from being submitted once the neuron count is large enough.

### Finding Description

In `rs/sns/governance/src/governance.rs`, the function `compute_ballots_for_new_proposal` (line 5226) iterates over the entire `self.proto.neurons` BTreeMap in a single synchronous `for` loop with no instruction-limit guard:

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

This function is called unconditionally from `make_proposal` (line 3457) via the internal call chain:

```
make_proposal → compute_ballots_for_new_proposal → for (k, v) in self.proto.neurons.iter()
```

The `NervousSystemParameters` allows up to `MAX_NUMBER_OF_NEURONS_CEILING = 200_000` neurons. The loop has no `is_over_instructions_limit()` check, no DTS (Deterministic Time Slicing) yield point, and no pagination. The NNS governance canister solved this exact problem by using a pre-computed `VotingPowerSnapshot` with a background timer task (`compute_ballots_for_standard_proposal` in NNS reads from a snapshot, not from a live neuron scan). The SNS governance canister has no equivalent mechanism — it scans all neurons live on every proposal submission.

By contrast, the NNS `cast_vote_and_cascade_follow` (in `rs/nns/governance/src/voting.rs`) explicitly uses `over_soft_message_limit()` checks and `noop_self_call_if_over_instructions` to break work across messages. The SNS `cast_vote_and_cascade_follow` (in `rs/sns/governance/src/governance.rs`, line 3687) also has no such guard — it runs its BFS loop to completion synchronously. Both paths are triggered by an unprivileged ingress sender.

The attacker-controlled entry path is:
1. Attacker (or organic growth) fills the SNS to near `max_number_of_neurons` (up to 200,000).
2. Any neuron holder calls `manage_neuron` → `MakeProposal`.
3. `make_proposal` calls `compute_ballots_for_new_proposal`, which iterates all 200,000 neurons.
4. The call exhausts the per-message instruction limit and traps.
5. No proposal can ever be submitted again, permanently freezing SNS governance.

### Impact Explanation

**Cycles/resource accounting bug leading to governance denial of service.** Once the neuron count is large enough, every `make_proposal` call traps due to instruction-limit exhaustion. This permanently prevents any new governance proposal from being submitted, including upgrade proposals. The SNS governance canister becomes unupgradeable and ungovernable. This is a permanent, irreversible freeze of the SNS governance system affecting all token holders.

### Likelihood Explanation

SNS instances with active communities can organically reach tens of thousands of neurons through swap participation (up to `MAX_NEURONS_FOR_DIRECT_PARTICIPANTS` neurons are created during the swap). An attacker who can stake tokens can also deliberately inflate the neuron count toward the ceiling. The ceiling of 200,000 is reachable in active SNS deployments. The vulnerability requires no privileged access — any principal with enough tokens to stake a neuron can trigger the condition.

### Recommendation

Replace the synchronous full-scan in `compute_ballots_for_new_proposal` with a pre-computed voting power snapshot approach analogous to the NNS governance implementation (`compute_ballots_for_standard_proposal` in NNS reads from `VOTING_POWER_SNAPSHOTS`). Alternatively, add instruction-limit checks inside the loop and split ballot computation across multiple messages using DTS or a timer-based approach, similar to how `cast_vote_and_cascade_follow` in NNS governance uses `noop_self_call_if_over_instructions`.

### Proof of Concept

**Root cause — unbounded loop:** [1](#0-0) 

**Called synchronously from `make_proposal`:** [2](#0-1) 

**`make_proposal` invokes `compute_ballots_for_new_proposal` with no guard:** [3](#0-2) 

**Maximum neuron ceiling that bounds the loop size:** [4](#0-3) 

**Default `max_number_of_neurons` set to 200,000:** [5](#0-4) 

**Contrast: NNS governance `cast_vote_and_cascade_follow` uses explicit instruction-limit checks and self-calls to avoid exhaustion:** [6](#0-5) 

**Contrast: NNS `compute_ballots_for_standard_proposal` reads from a pre-computed snapshot, not a live scan:** [7](#0-6)

### Citations

**File:** rs/sns/governance/src/governance.rs (L3457-3462)
```rust
    pub async fn make_proposal(
        &mut self,
        proposer_id: &NeuronId,
        caller: &PrincipalId,
        proposal: &Proposal,
    ) -> Result<ProposalId, GovernanceError> {
```

**File:** rs/sns/governance/src/governance.rs (L5226-5227)
```rust
    fn compute_ballots_for_new_proposal(&self) -> Result<(u64, BTreeMap<String, Ballot>), String> {
        let now_seconds = self.env.now();
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

**File:** rs/sns/governance/src/types.rs (L383-386)
```rust
    /// This is an upper bound for `max_number_of_neurons`. Exceeding it may cause
    /// degradation in the governance canister or the subnet hosting the SNS.
    /// See also: `MAX_NEURONS_FOR_DIRECT_PARTICIPANTS`.
    pub const MAX_NUMBER_OF_NEURONS_CEILING: u64 = 200_000;
```

**File:** rs/sns/governance/src/types.rs (L478-478)
```rust
            max_number_of_neurons: Some(200_000),
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

**File:** rs/nns/governance/src/governance.rs (L5486-5533)
```rust
    fn compute_ballots_for_standard_proposal(
        &self,
        now_seconds: u64,
    ) -> Result<
        (
            HashMap<u64, Ballot>,
            u64,         /*potential_voting_power*/
            Option<u64>, /*previous_ballots_timestamp_seconds*/
        ),
        GovernanceError,
    > {
        let current_voting_power_snapshot = self
            .neuron_store
            .compute_voting_power_snapshot_for_standard_proposal(
                self.voting_power_economics(),
                now_seconds,
            )?;

        // Check if there is a voting power spike. If there is, then the return value here
        // will be `Some(...)`.
        let maybe_previous_ballots_if_voting_power_spike_detected = VOTING_POWER_SNAPSHOTS
            .with_borrow(|snapshots| {
                snapshots.previous_ballots_if_voting_power_spike_detected(
                    current_voting_power_snapshot.total_potential_voting_power(),
                    now_seconds,
                )
            });

        let (voting_power_snapshot, previous_ballots_timestamp_seconds) =
            match maybe_previous_ballots_if_voting_power_spike_detected {
                // This is the extraordinary case - we have a voting power spike, and we
                // need to use the previous snapshot.
                Some((previous_snapshot_timestamp, previous_snapshot)) => {
                    (previous_snapshot, Some(previous_snapshot_timestamp))
                }
                // This is the normal case - we have no voting power spike, so we use the
                // current snapshot.
                None => (current_voting_power_snapshot, None),
            };

        let (ballots, total_potential_voting_power) =
            voting_power_snapshot.create_ballots_and_total_potential_voting_power();
        Ok((
            ballots,
            total_potential_voting_power,
            previous_ballots_timestamp_seconds,
        ))
    }
```
