### Title
Unbounded Neuron Iteration in `compute_ballots_for_new_proposal` Exhausts Instruction Limit - (`rs/sns/governance/src/governance.rs`)

### Summary
The SNS Governance canister's `compute_ballots_for_new_proposal` function iterates synchronously over every neuron in `self.proto.neurons` (a heap-resident `BTreeMap`) with no instruction-limit guard. Any neuron holder can trigger this path via `make_proposal`. When the neuron population approaches `max_number_of_neurons`, the iteration exhausts the per-message instruction budget, causing the canister to trap and permanently blocking proposal submission.

### Finding Description

`compute_ballots_for_new_proposal` in the SNS Governance canister performs an unbounded linear scan over all neurons:

```rust
for (k, v) in self.proto.neurons.iter() {
    if v.dissolve_delay_seconds(now_seconds) < min_dissolve_delay_for_vote {
        continue;
    }
    let voting_power = v.voting_power(...);
    total_power += voting_power as u128;
    electoral_roll.insert(k.clone(), Ballot { ... });
}
``` [1](#0-0) 

This function is called synchronously inside `make_proposal` before any state is mutated:

```rust
let (_, electoral_roll) = self
    .compute_ballots_for_new_proposal()
    .map_err(...)?;
``` [2](#0-1) 

Unlike the NNS Governance canister, which was updated to use `compute_voting_power_snapshot_for_standard_proposal` over stable-memory neurons and wraps its voting cascade with `noop_self_call_if_over_instructions`, the SNS Governance canister has **no equivalent instruction-limit protection** in this path. A grep for `noop_self_call_if_over_instructions` in `rs/sns/governance/` returns zero matches.

The NNS Governance changelog explicitly documents the problem and the mitigations applied there:

> "Unstaking maturity task will be processing up to 100 neurons in a single message, to avoid exceeding the instruction limit in a single execution."
> "Avoid applying `approve_genesis_kyc` to an unbounded number of neurons, but at most 1000 neurons." [3](#0-2) 

The SNS Governance canister stores neurons in `self.proto.neurons`, a heap `BTreeMap<String, Neuron>`. The `max_number_of_neurons` parameter is configurable by governance up to `MAX_NUMBER_OF_NEURONS_CEILING`, defined in `rs/sns/governance/src/types.rs`. [4](#0-3) 

The NNS Governance benchmark shows that with only 100 neurons, `compute_ballots_for_new_proposal` already consumes ~2.45 million instructions. [5](#0-4)  The IC per-message instruction limit is 5 billion. Scaling linearly, ~200,000 neurons would exhaust the budget. The NNS governance's own benchmark explicitly projects against `MAX_NUMBER_OF_NEURONS` to verify safety: [6](#0-5)  — no equivalent benchmark or projection exists for the SNS governance path.

The `check_neuron_population_can_grow` guard only prevents adding neurons beyond `max_number_of_neurons`; it does not protect the iteration cost of `compute_ballots_for_new_proposal`. [7](#0-6) 

### Impact Explanation

If the SNS neuron population is large enough (depending on the SNS-specific `max_number_of_neurons` ceiling), every call to `make_proposal` will trap with `CanisterInstructionLimitExceeded`. This permanently blocks all proposal submission in the SNS governance canister. Since SNS upgrades and parameter changes require proposals, the canister becomes unupgradeable and ungovernable — a complete governance DoS.

### Likelihood Explanation

Any principal holding SNS tokens can stake neurons. An attacker (or organic growth) can fill the neuron population to `max_number_of_neurons`. Once that threshold is reached, the next `make_proposal` call by any legitimate user triggers the trap. The attacker does not need any privileged role; only the ability to stake tokens and submit a proposal. The entry point is the public `manage_neuron` update call with a `MakeProposal` command.

### Recommendation

1. Add an instruction-limit guard to `compute_ballots_for_new_proposal` analogous to the NNS Governance's `noop_self_call_if_over_instructions` pattern, or move ballot computation to a timer-based async task.
2. Benchmark `compute_ballots_for_new_proposal` against the actual `MAX_NUMBER_OF_NEURONS_CEILING` for SNS (as the NNS governance does in its `check_projected_instructions` bench) and enforce that the projected instruction count stays well below the 5B limit.
3. Consider migrating SNS neuron storage to stable memory (as NNS did) to reduce per-neuron iteration cost.

### Proof of Concept

1. Deploy an SNS with `max_number_of_neurons` set to a large value (e.g., the ceiling).
2. Stake SNS tokens across many principals until `self.proto.neurons.len()` approaches `max_number_of_neurons`.
3. Call `manage_neuron` with `Command::MakeProposal(...)` from any neuron holder.
4. `make_proposal` calls `compute_ballots_for_new_proposal` at [1](#0-0) , iterating all neurons synchronously.
5. The message traps with `CanisterInstructionLimitExceeded`; no proposal can ever be submitted again.

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

**File:** rs/sns/governance/src/governance.rs (L6365-6379)
```rust
    fn check_neuron_population_can_grow(&self) -> Result<(), GovernanceError> {
        let max_number_of_neurons = self
            .nervous_system_parameters_or_panic()
            .max_number_of_neurons
            .expect("NervousSystemParameters must have max_number_of_neurons");

        if (self.proto.neurons.len() as u64) + 1 > max_number_of_neurons {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "Cannot add neuron. Max number of neurons reached.",
            ));
        }

        Ok(())
    }
```

**File:** rs/nns/governance/CHANGELOG.md (L656-675)
```markdown
          multiple messages.
        * Unstaking maturity task has a limit of 100 neurons per message, which prevents it from
          exceeding instruction limit.
        * The execution of `ApproveGenesisKyc` proposals have a limit of 1000 neurons, above which
          the proposal will fail.
        * More benchmarks were added.
* Enable timer task metrics for better observability.

## Changed

* Voting Rewards will be scheduled by a timer instead of by heartbeats.
* Unstaking maturity task will be processing up to 100 neurons in a single message, to avoid
  exceeding the instruction limit in a single execution.
* Voting Rewards will be distributed asynchronously in the background after being calculated.
    * This will allow rewards to be compatible with neurons being stored in Stable Memory.
* Ramp up the failure rate of _pb method to 0.7 again.

## Fixed

* Avoid applying `approve_genesis_kyc` to an unbounded number of neurons, but at most 1000 neurons.
```

**File:** rs/sns/governance/src/gen/ic_sns_governance.pb.v1.rs (L1703-1710)
```rust
    pub default_followees: ::core::option::Option<DefaultFollowees>,
    /// The maximum number of allowed neurons. When this maximum is reached, no new
    /// neurons will be created until some are removed.
    ///
    /// This number must be larger than zero and at most as large as the defined
    /// ceiling MAX_NUMBER_OF_NEURONS_CEILING.
    #[prost(uint64, optional, tag = "7")]
    pub max_number_of_neurons: ::core::option::Option<u64>,
```

**File:** rs/nns/governance/canbench/canbench_results.yml (L44-50)
```yaml
  compute_ballots_for_new_proposal_with_stable_neurons:
    total:
      calls: 1
      instructions: 2450000
      heap_increase: 0
      stable_memory_increase: 256
    scopes: {}
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
