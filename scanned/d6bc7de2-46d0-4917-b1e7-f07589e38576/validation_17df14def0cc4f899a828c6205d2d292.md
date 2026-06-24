### Title
Unbounded Neuron Iteration in SNS `compute_ballots_for_new_proposal` Can Exhaust Instruction Limit, Permanently Blocking Proposal Submission - (File: rs/sns/governance/src/governance.rs)

---

### Summary

The SNS Governance canister's `compute_ballots_for_new_proposal` function iterates synchronously over every neuron in `self.proto.neurons` with no instruction-limit guard. When the neuron count is large enough, a call to `make_proposal` will trap with `CanisterInstructionLimitExceeded`, making it impossible for any neuron holder to submit new proposals to that SNS.

---

### Finding Description

`make_proposal` in `rs/sns/governance/src/governance.rs` calls `compute_ballots_for_new_proposal` unconditionally before inserting the proposal: [1](#0-0) 

`compute_ballots_for_new_proposal` contains a plain `for` loop over the entire neuron map: [2](#0-1) 

Per iteration the function reads the neuron's dissolve state, computes a multi-factor voting-power bonus, and inserts a `Ballot` into a `BTreeMap`. There is no call to `ic_cdk::api::instruction_counter()`, no `over_soft_message_limit()` check, and no `noop_self_call_if_over_instructions` escape hatch anywhere in this code path. [3](#0-2) 

By contrast, the NNS Governance canister explicitly defines hard and soft instruction limits for its voting cascade and uses `noop_self_call_if_over_instructions` to yield before the limit is hit: [4](#0-3) [5](#0-4) 

The NNS also uses a pre-computed voting-power snapshot rather than iterating live neurons at proposal time. The SNS has neither protection.

The SNS `NervousSystemParameters` allows up to `MAX_NUMBER_OF_NEURONS` neurons (defined in `rs/sns/governance/src/types.rs`). The IC instruction limit for a single update message on a system subnet is 5 × 10⁹ instructions. With a sufficiently large neuron population the per-neuron work (dissolve-delay arithmetic, age-bonus computation, `BTreeMap::insert`) will exhaust that budget.

---

### Impact Explanation

Once the neuron count crosses the threshold, every call to `make_proposal` traps. No new governance proposals can be submitted to the SNS. Ongoing proposals are unaffected, but the SNS governance is permanently frozen for new actions — including any remediation proposal that would require `make_proposal` to succeed. This is a complete denial-of-service on SNS governance proposal submission.

---

### Likelihood Explanation

Any SNS that achieves organic growth in its neuron population (e.g., a popular DeFi or gaming SNS) will naturally approach the limit over time. An adversary who can cheaply create neurons (by splitting existing ones or staking small amounts) can accelerate this. The `make_proposal` entry point requires only that the caller hold a neuron with `SubmitProposal` permission and sufficient stake to cover `reject_cost_e8s` — a low bar for any legitimate participant. No privileged access, no threshold corruption, and no external oracle is required.

---

### Recommendation

1. **Adopt the NNS pattern**: replace the live neuron iteration with a pre-computed, periodically refreshed voting-power snapshot (analogous to `compute_voting_power_snapshot_for_standard_proposal` in the NNS neuron store). The snapshot can be updated in a timer job where instruction overruns are safe to handle incrementally.

2. **Add an instruction-limit guard**: if the live iteration must be kept, add an `ic_cdk::api::instruction_counter()` check inside the loop and return an error (not a trap) when the soft limit is approached, so the canister remains responsive.

3. **Enforce a tighter neuron cap**: lower `MAX_NUMBER_OF_NEURONS` to a value that is provably safe given the per-neuron instruction cost, or document and test the exact safe upper bound.

---

### Proof of Concept

1. Deploy an SNS with default `NervousSystemParameters`.
2. Create N neurons (by staking and claiming) where N is large enough that the per-neuron instruction cost × N > 5 × 10⁹ (the system-subnet update-message limit). Based on the per-iteration work (dissolve-delay read + voting-power computation + `BTreeMap::insert`), this threshold is reachable well within the documented `MAX_NUMBER_OF_NEURONS` ceiling.
3. Call `manage_neuron` → `MakeProposal` from any neuron with `SubmitProposal` permission.
4. Observe the call trap with `CanisterInstructionLimitExceeded`.
5. Confirm that no subsequent `make_proposal` call succeeds regardless of the proposer, because the neuron map size is unchanged.

The root cause is the unbounded synchronous loop at: [2](#0-1) 

called unconditionally from: [1](#0-0)

### Citations

**File:** rs/sns/governance/src/governance.rs (L3557-3559)
```rust
        let (_, electoral_roll) = self
            .compute_ballots_for_new_proposal()
            .map_err(|err| GovernanceError::new_with_message(ErrorType::PreconditionFailed, err))?;
```

**File:** rs/sns/governance/src/governance.rs (L5225-5295)
```rust
    /// Computes the total potential voting power of the governance canister and ballots.
    fn compute_ballots_for_new_proposal(&self) -> Result<(u64, BTreeMap<String, Ballot>), String> {
        let now_seconds = self.env.now();

        let nervous_system_parameters = self.nervous_system_parameters_or_panic();

        // Voting power bonus parameters.
        let max_dissolve_delay = nervous_system_parameters
            .max_dissolve_delay_seconds
            .expect("NervousSystemParameters must have max_dissolve_delay_seconds");

        let max_age_bonus = nervous_system_parameters
            .max_neuron_age_for_age_bonus
            .expect("NervousSystemParameters must have max_neuron_age_for_age_bonus");

        let max_dissolve_delay_bonus_percentage = nervous_system_parameters
            .max_dissolve_delay_bonus_percentage
            .expect("NervousSystemParameters must have max_dissolve_delay_bonus_percentage");

        let max_age_bonus_percentage = nervous_system_parameters
            .max_age_bonus_percentage
            .expect("NervousSystemParameters must have max_age_bonus_percentage");

        let min_dissolve_delay_for_vote = nervous_system_parameters
            .neuron_minimum_dissolve_delay_to_vote_seconds
            .expect("NervousSystemParameters must have min_dissolve_delay_for_vote");

        let mut electoral_roll = BTreeMap::<String, Ballot>::new();
        let mut total_power: u128 = 0;

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

        if total_power >= (u64::MAX as u128) {
            // The way the neurons are configured, the total voting
            // power on this proposal would overflow a u64!
            return Err("Voting power overflow.".to_string());
        }
        if electoral_roll.is_empty() {
            // Cannot make a proposal with no eligible voters.  This
            // is a precaution that shouldn't happen as we check that
            // the voter is allowed to vote.
            return Err("No eligible voters.".to_string());
        }

        Ok((total_power as u64, electoral_roll))
    }
```

**File:** rs/nns/governance/src/voting.rs (L22-31)
```rust
/// The hard limit for the number of instructions that can be executed in a single call context.
/// This leaves room for 750 thousand neurons with complex following.
const HARD_VOTING_INSTRUCTIONS_LIMIT: u64 = 750 * BILLION;
// For production, we want this higher so that we can process more votes, but without affecting
// the overall responsiveness of the canister. 1 Billion seems like a reasonable compromise.
const SOFT_VOTING_INSTRUCTIONS_LIMIT: u64 = if cfg!(feature = "test") {
    1_000_000
} else {
    BILLION
};
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
