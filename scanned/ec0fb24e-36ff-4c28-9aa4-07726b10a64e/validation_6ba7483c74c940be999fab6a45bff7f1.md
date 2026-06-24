### Title
Unbounded Neuron Iteration in `compute_ballots_for_new_proposal` Exhausts Instruction Limit, Bricking SNS Governance Proposals - (File: `rs/sns/governance/src/governance.rs`)

---

### Summary

The SNS governance canister's `make_proposal` update call synchronously iterates over **all neurons** in `self.proto.neurons` inside `compute_ballots_for_new_proposal` with no instruction-limit guard. On an application subnet the per-message instruction cap is 40 billion. If an SNS accumulates enough neurons, every `make_proposal` call will trap with `CanisterInstructionLimitExceeded`, permanently preventing new governance proposals from being submitted.

---

### Finding Description

`compute_ballots_for_new_proposal` in the SNS governance canister performs an unbounded `for (k, v) in self.proto.neurons.iter()` loop to build the electoral roll for every new proposal: [1](#0-0) 

There is no instruction counter check, no early exit, and no batching. The entire neuron map is traversed in a single synchronous execution slice.

This function is called directly from the externally reachable `make_proposal` update handler: [2](#0-1) 

The application-subnet instruction limit is: [3](#0-2) 

The NNS governance already recognized this exact problem and resolved it by replacing the equivalent loop with a pre-computed voting-power snapshot (`compute_ballots_for_standard_proposal` → `compute_voting_power_snapshot_for_standard_proposal`). The SNS governance has not received the same fix and still uses the raw heap-map iteration. [4](#0-3) 

---

### Impact Explanation

Once the SNS neuron count grows large enough that iterating all neurons in one message exceeds 40 billion instructions, **every** `make_proposal` call traps. No new governance proposals can be submitted. The SNS DAO is effectively frozen: no parameter changes, no treasury actions, no upgrades can be proposed. The condition is permanent unless the canister is upgraded via an out-of-band mechanism (e.g., NNS root intervention), which itself may be unavailable if the SNS is fully decentralized.

---

### Likelihood Explanation

Any unprivileged principal holding a valid SNS neuron can call `make_proposal`. The neuron count grows organically as users stake tokens; popular SNS projects can accumulate tens of thousands of neurons. The NNS governance benchmark projects that iterating neurons at scale approaches the 25-billion-instruction mark even with the optimized stable-memory path: [5](#0-4) 

The SNS path uses the slower heap `BTreeMap` iteration (`self.proto.neurons`), making it more instruction-intensive per neuron. No attacker action is required; organic growth of the SNS community is sufficient to trigger the condition.

---

### Recommendation

Replace the unbounded loop in `compute_ballots_for_new_proposal` with the same voting-power snapshot mechanism already used by NNS governance (`compute_voting_power_snapshot_for_standard_proposal`). Alternatively, enforce a hard cap on `self.proto.neurons.len()` inside the `claim_swap_neurons` / neuron-creation path, and add an instruction-counter guard (analogous to `noop_self_call_if_over_instructions`) so that if the limit is approached the call fails gracefully rather than trapping.

---

### Proof of Concept

1. Deploy an SNS with default parameters.
2. Have a large number of principals stake tokens and claim neurons until `self.proto.neurons.len()` is large enough that iterating all of them in one message exceeds 40 billion instructions.
3. Call `make_proposal` from any valid neuron holder.
4. Observe the call rejected with `CanisterInstructionLimitExceeded` (error code confirmed at): [6](#0-5) 

5. Repeat — every subsequent `make_proposal` call will fail identically, confirming the governance canister is permanently unable to accept new proposals.

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

**File:** rs/config/src/subnet_config.rs (L34-36)
```rust
// The limit on the number of instructions a message is allowed to executed.
// Going above the limit results in an `InstructionLimitExceeded` error.
pub(crate) const MAX_INSTRUCTIONS_PER_MESSAGE: NumInstructions = NumInstructions::new(40 * B);
```

**File:** rs/nns/governance/src/governance.rs (L5486-5502)
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
```

**File:** rs/nns/governance/src/governance/benches.rs (L467-478)
```rust
    let bench_result = bench_fn(|| {
        governance
            .compute_ballots_for_standard_proposal(123_456_789)
            .expect("Failed!");
    });

    check_projected_instructions(
        bench_result,
        num_neurons,
        MAX_NUMBER_OF_NEURONS as u64,
        25_000_000_000,
    )
```
