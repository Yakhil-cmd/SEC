### Title
Unbounded Loop Over All Neurons in `compute_ballots_for_new_proposal()` Can Cause DoS in SNS Governance Canister - (File: `rs/sns/governance/src/governance.rs`)

---

### Summary

The SNS Governance canister's `compute_ballots_for_new_proposal()` function iterates synchronously over every neuron in `self.proto.neurons` without any instruction-limit guard. This function is called directly inside the `make_proposal()` update handler, which is reachable by any neuron holder with `SubmitProposal` permission. As the neuron population grows toward the SNS-configured maximum, the loop's instruction cost grows linearly and can exceed the IC's per-message instruction limit, permanently preventing new proposals from being submitted and freezing SNS governance.

---

### Finding Description

**Root cause — unbounded loop with no instruction-limit check:**

In `rs/sns/governance/src/governance.rs` at line 5255, `compute_ballots_for_new_proposal()` iterates the entire in-memory neuron map:

```rust
for (k, v) in self.proto.neurons.iter() {
    if v.dissolve_delay_seconds(now_seconds) < min_dissolve_delay_for_vote {
        continue;
    }
    let voting_power = v.voting_power(
        now_seconds, max_dissolve_delay, max_age_bonus,
        max_dissolve_delay_bonus_percentage, max_age_bonus_percentage,
    );
    total_power += voting_power as u128;
    electoral_roll.insert(k.clone(), Ballot { vote: Vote::Unspecified as i32, voting_power, cast_timestamp_seconds: 0 });
}
```

There is no call to `ic_cdk::api::instruction_counter()`, no early-exit, and no batching. The loop runs to completion in a single message execution.

**Call path — reachable by any unprivileged neuron holder:**

`make_proposal()` (line 3457) calls `compute_ballots_for_new_proposal()` at line 3557–3559 synchronously, before any state mutation:

```rust
let (_, electoral_roll) = self
    .compute_ballots_for_new_proposal()
    .map_err(|err| GovernanceError::new_with_message(ErrorType::PreconditionFailed, err))?;
```

`make_proposal()` is an update call handler. Any principal holding a neuron with `SubmitProposal` permission and sufficient stake can invoke it.

**Contrast with NNS Governance — the fix already exists there:**

NNS Governance has already addressed this exact pattern. Its `compute_ballots_for_standard_proposal()` (line 5486) no longer iterates neurons directly; it reads from a pre-computed voting-power snapshot maintained by a timer task:

```rust
let current_voting_power_snapshot = self
    .neuron_store
    .compute_voting_power_snapshot_for_standard_proposal(
        self.voting_power_economics(),
        now_seconds,
    )?;
```

The NNS CHANGELOG entry for proposal 135702 explicitly documents the motivation: *"Voting Rewards will be distributed asynchronously in the background after being calculated … to avoid exceeding the instruction limit in a single execution."* The SNS canister has not received the equivalent refactor.

**Instruction-cost projection:**

The NNS governance benchmark `compute_ballots_for_new_proposal_with_stable_neurons` (line 441 of `rs/nns/governance/src/governance/benches.rs`) measures 2,450,000 instructions for 100 neurons stored in stable memory and projects the cost to `MAX_NUMBER_OF_NEURONS` against a 25-billion-instruction budget. SNS neurons are stored in heap memory (`BTreeMap<String, Neuron>`), which is cheaper per access, but the SNS `max_number_of_neurons` ceiling is a governance-controlled parameter with no hard upper bound enforced at the replica level. A sufficiently large SNS (or one whose `max_number_of_neurons` is set high) will push the single-message cost past the 40-billion-instruction update-call limit.

---

### Impact Explanation

When the instruction limit is exceeded, `make_proposal()` returns `CanisterInstructionLimitExceeded` for every caller. Because ballot creation is a prerequisite for any proposal, **no new governance proposals can be submitted**. This freezes the SNS: upgrades, parameter changes, treasury transfers, and emergency actions all require a proposal. The condition is self-reinforcing — the canister cannot be upgraded to fix itself because the upgrade proposal cannot be submitted.

---

### Likelihood Explanation

- **Attacker-controlled entry path:** Any principal with a neuron holding `SubmitProposal` permission can call `manage_neuron` → `MakeProposal`. No privileged role is required.
- **Neuron accumulation:** SNS neurons accumulate naturally as users stake tokens. An adversary can also deliberately stake many small neurons (up to `max_number_of_neurons`) to push the canister toward the threshold.
- **No existing mitigation in SNS:** Unlike NNS Governance, the SNS canister has no snapshot mechanism, no instruction-limit guard inside the loop, and no timer-based ballot pre-computation.
- **Governance-controlled ceiling:** The `max_number_of_neurons` parameter is set by governance proposals; a community that raises it without understanding the instruction-cost implication accelerates the risk.

---

### Recommendation

1. **Adopt the NNS snapshot pattern:** Compute voting-power snapshots in a recurring timer task (as NNS Governance does) and read from the snapshot inside `make_proposal()` instead of iterating all neurons synchronously.
2. **Add an instruction-limit guard as an interim measure:** Insert `ic_cdk::api::instruction_counter()` checks inside the loop and return an error (or defer to a timer) if the soft limit is approached, mirroring the pattern used in `NeuronRangeValidationTask::validate_next_chunk()` and `PruneFollowingTask`.
3. **Add a benchmark:** Add a `compute_ballots_for_new_proposal` canbench entry for SNS governance (analogous to the NNS one) that projects instruction cost to `max_number_of_neurons` and enforces a ceiling, so regressions are caught in CI.

---

### Proof of Concept

1. Deploy an SNS with `max_number_of_neurons` set to a large value (e.g., 100,000).
2. Stake tokens and claim neurons until the neuron count approaches the maximum. Each `claim_or_refresh_neuron` call is permissionless for any token holder.
3. Attempt to call `manage_neuron` with `Command::MakeProposal(...)` from any neuron with `SubmitProposal` permission.
4. Observe that the call fails with `CanisterInstructionLimitExceeded` because `compute_ballots_for_new_proposal()` exhausts the 40-billion-instruction budget iterating all neurons.
5. Confirm that no proposal can be submitted from any neuron, freezing governance. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** rs/sns/governance/src/governance.rs (L6363-6379)
```rust
    /// Checks whether new neurons can be added or whether the maximum number of neurons,
    /// as defined in the nervous system parameters, has already been reached.
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

**File:** rs/nns/governance/src/governance/benches.rs (L441-479)
```rust
fn compute_ballots_for_new_proposal_with_stable_neurons() -> BenchResult {
    let now_seconds = 1732817584;
    let num_neurons = 100;

    let mut governance = Governance::new(
        Default::default(),
        Arc::new(MockEnvironment::new(vec![], now_seconds)),
        Arc::new(StubIcpLedger {}),
        Arc::new(StubCMC {}),
        Box::new(MockRandomness::new()),
    );

    for id in 1..=num_neurons {
        governance
            .add_neuron(
                id,
                make_neuron(
                    id,
                    PrincipalId::new_user_test_id(id),
                    1_000_000_000,
                    hashmap! {}, // get the default followees
                ),
            )
            .unwrap();
    }

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
}
```
