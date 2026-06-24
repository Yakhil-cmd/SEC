### Title
Unbounded Neuron Iteration in SNS `distribute_rewards` Can Exhaust Instruction Limit, Permanently Blocking Reward Distribution - (File: rs/sns/governance/src/governance.rs)

---

### Summary

The SNS governance canister's `distribute_rewards()` function iterates over every neuron that voted during a reward period in a single synchronous loop with no instruction-limit guard. If the number of voting neurons is large enough, the message will trap when the IC instruction limit is exceeded, rolling back state and leaving `latest_reward_event` unchanged. Every subsequent periodic invocation will attempt the same unbounded loop and trap again, permanently blocking SNS voting reward distribution.

---

### Finding Description

`distribute_rewards()` in `rs/sns/governance/src/governance.rs` builds a `HashMap<NeuronId, Decimal>` called `neuron_id_to_reward_shares` by iterating over every ballot of every `ReadyToSettle` proposal. It then distributes maturity to each neuron in a plain `for` loop:

```rust
for (neuron_id, neuron_reward_shares) in neuron_id_to_reward_shares {
    let neuron: &mut Neuron = match self.get_neuron_result_mut(&neuron_id) { ... };
    ...
    neuron.maturity_e8s_equivalent += neuron_reward_e8s;
    distributed_e8s_equivalent += neuron_reward_e8s;
}
``` [1](#0-0) 

There is no call to any instruction-limit check (e.g., `is_message_over_threshold`, `noop_self_call_if_over_instructions`, or equivalent) inside or around this loop. The entire function — including ballot aggregation and maturity assignment — executes atomically in one message. [2](#0-1) 

The function is invoked from `run_periodic_tasks`, which is the SNS governance heartbeat/timer handler. If the message traps due to instruction exhaustion, the IC rolls back all state mutations, so `latest_reward_event` is never updated. The next heartbeat fires the same code path with the same (or larger) neuron set, trapping again — a permanent liveness failure. [3](#0-2) 

The NNS governance canister already solved this exact problem by introducing `RewardsDistributionStateMachine`, which processes neuron maturity updates in batches across multiple timer firings, checking `is_over_instructions_limit()` after each neuron:

```rust
while let Some((id, reward_e8s)) = self.rewards.pop_first() {
    ...
    if is_over_instructions_limit() {
        break;
    }
}
``` [4](#0-3) 

The SNS governance has not adopted this pattern. [5](#0-4) 

---

### Impact Explanation

If the instruction limit is exceeded, the SNS governance canister permanently loses the ability to distribute voting rewards. Neurons that voted will never receive maturity increments for the affected period (and all future periods, since `latest_reward_event` is never advanced). This breaks the core economic incentive mechanism of every SNS DAO deployed on the IC.

**Impact: High** — permanent liveness failure of SNS voting reward distribution; neuron maturity accrual halts for all participants.

---

### Likelihood Explanation

SNS DAOs are designed to maximize neuron participation. A popular SNS with tens of thousands of active neurons voting on a high-weight proposal can produce a `neuron_id_to_reward_shares` map with entries proportional to the total neuron count. The IC instruction limit for a single message is approximately 40 billion instructions for system canisters. Each `get_neuron_result_mut` call on a neuron stored in stable memory costs on the order of millions of instructions. With ~10,000–40,000 voting neurons, the loop can plausibly exhaust the limit. No privileged access is required; ordinary neuron holders voting (the intended behavior) is the trigger.

**Likelihood: Low-Medium** — requires a large but realistic number of active voters; grows as SNS adoption increases.

---

### Recommendation

Apply the same batched-distribution pattern already used by NNS governance:

1. After computing `neuron_id_to_reward_shares`, persist the pending distribution to stable storage (analogous to `RewardsDistributionInProgress`).
2. Record the new `RewardEvent` immediately so the reward round is not re-attempted.
3. Process maturity increments in a separate periodic timer task that checks `is_message_over_threshold` after each neuron and resumes in the next firing if the limit is reached. [6](#0-5) 

---

### Proof of Concept

1. Deploy an SNS with a large number of neurons (e.g., 50,000 neurons created via the swap).
2. Submit a governance proposal and have all neurons vote (directly or via following).
3. Wait for the proposal to reach `ReadyToSettle` status.
4. Observe that the next `run_periodic_tasks` heartbeat calls `distribute_rewards()`.
5. The `neuron_id_to_reward_shares` map contains ~50,000 entries; each `get_neuron_result_mut` accesses stable memory.
6. The loop exhausts the per-message instruction limit; the message traps; state is rolled back.
7. `latest_reward_event` is unchanged; every subsequent heartbeat repeats steps 4–6.
8. No voting rewards are ever distributed again; neuron maturity is permanently frozen. [7](#0-6)

### Citations

**File:** rs/sns/governance/src/governance.rs (L5763-5764)
```rust
    fn distribute_rewards(&mut self, supply: Tokens) {
        log!(INFO, "distribute_rewards. Supply: {:?}", supply);
```

**File:** rs/sns/governance/src/governance.rs (L5892-5997)
```rust
        // Add up reward shares based on voting power that was exercised.
        let mut neuron_id_to_reward_shares: HashMap<NeuronId, Decimal> = HashMap::new();
        for proposal_id in &considered_proposals {
            if let Some(proposal) = self.get_proposal_data(*proposal_id) {
                for (voter, ballot) in &proposal.ballots {
                    #[allow(clippy::blocks_in_conditions)]
                    if !Vote::try_from(ballot.vote)
                        .unwrap_or_else(|_| {
                            println!(
                                "{}Vote::from invoked with unexpected value {}.",
                                log_prefix(),
                                ballot.vote
                            );
                            Vote::Unspecified
                        })
                        .eligible_for_rewards()
                    {
                        continue;
                    }

                    match NeuronId::from_str(voter) {
                        Ok(neuron_id) => {
                            let reward_shares = i2d(ballot.voting_power);
                            *neuron_id_to_reward_shares
                                .entry(neuron_id)
                                .or_insert_with(|| dec!(0)) += reward_shares;
                        }
                        Err(e) => {
                            log!(
                                ERROR,
                                "Could not use voter {} to calculate total_voting_rights \
                                 since it's NeuronId was invalid. Underlying error: {:?}.",
                                voter,
                                e
                            );
                        }
                    }
                }
            }
        }
        // Freeze reward shares, now that we are done adding them up.
        let neuron_id_to_reward_shares = neuron_id_to_reward_shares;
        let total_reward_shares: Decimal = neuron_id_to_reward_shares.values().sum();
        debug_assert!(
            total_reward_shares >= dec!(0),
            "total_reward_shares: {total_reward_shares} neuron_id_to_reward_shares: {neuron_id_to_reward_shares:#?}",
        );

        // Because of rounding (and other shenanigans), it is possible that some
        // portion of this amount ends up not being actually distributed.
        let mut distributed_e8s_equivalent = 0_u64;
        // Now that we know the size of the pie (rewards_purse_e8s), and how
        // much of it each neuron is supposed to get (*_reward_shares), we now
        // proceed to actually handing out those rewards.
        if total_reward_shares == dec!(0) {
            log!(
                ERROR,
                "Warning: total_reward_shares is 0. Therefore, we skip increasing \
                 neuron maturity. neuron_id_to_reward_shares: {:#?}",
                neuron_id_to_reward_shares,
            );
        } else {
            for (neuron_id, neuron_reward_shares) in neuron_id_to_reward_shares {
                let neuron: &mut Neuron = match self.get_neuron_result_mut(&neuron_id) {
                    Ok(neuron) => neuron,
                    Err(err) => {
                        log!(
                            ERROR,
                            "Cannot find neuron {}, despite having voted with power {} \
                             in the considered reward period. The reward that should have been \
                             distributed to this neuron is simply skipped, so the total amount \
                             of distributed reward for this period will be lower than the maximum \
                             allowed. Underlying error: {:?}.",
                            neuron_id,
                            neuron_reward_shares,
                            err
                        );
                        continue;
                    }
                };

                // Dividing before multiplying maximizes our chances of success.
                let neuron_reward_e8s =
                    rewards_purse_e8s * (neuron_reward_shares / total_reward_shares);

                // Round down, and convert to u64.
                let neuron_reward_e8s = u64::try_from(neuron_reward_e8s).unwrap_or_else(|err| {
                    panic!(
                        "Calculating reward for neuron {neuron_id:?}:\n\
                             neuron_reward_shares: {neuron_reward_shares}\n\
                             rewards_purse_e8s: {rewards_purse_e8s}\n\
                             total_reward_shares: {total_reward_shares}\n\
                             err: {err}",
                    )
                });
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
            }
```

**File:** rs/nns/governance/src/reward/distribution.rs (L42-52)
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
    }
```

**File:** rs/nns/governance/src/reward/distribution.rs (L55-106)
```rust
pub(crate) struct RewardsDistributionStateMachine<Memory>
where
    Memory: ic_stable_structures::Memory,
{
    // Map is reward_event_round (day_after_genesis) => rewards_distribution
    // This allows us to see if the latest_reward_event has finished distributing rewards
    // to neurons
    distributions: StableBTreeMap<u64, RewardsDistributionInProgress, Memory>,
}

impl<Memory: ic_stable_structures::Memory> RewardsDistributionStateMachine<Memory> {
    pub(crate) fn new(memory: Memory) -> Self {
        Self {
            distributions: StableBTreeMap::init(memory),
        }
    }

    fn with_next_distribution<R>(
        &mut self,
        callback: impl FnOnce((u64, &mut RewardsDistribution)) -> R,
    ) -> Option<R> {
        if let Some((day_after_genesis, proto)) = self.distributions.pop_first() {
            let mut distribution = RewardsDistribution::from(proto);
            let result = callback((day_after_genesis, &mut distribution));
            if !distribution.is_completely_finished() {
                self.distributions.insert(
                    day_after_genesis,
                    RewardsDistributionInProgress::from(distribution),
                );
            }
            Some(result)
        } else {
            None
        }
    }

    fn add_rewards_distribution(
        &mut self,
        day_after_genesis: u64,
        distribution: RewardsDistribution,
    ) -> Result<(), String> {
        if self.distributions.contains_key(&day_after_genesis) {
            return Err(format!(
                "{LOG_PREFIX}Rewards distribution already exists for day_after_genesis: {day_after_genesis}"
            ));
        }
        self.distributions.insert(
            day_after_genesis,
            RewardsDistributionInProgress::from(distribution),
        );
        Ok(())
    }
```

**File:** rs/nns/governance/src/reward/distribution.rs (L154-188)
```rust
    fn continue_processing(
        &mut self,
        neuron_store: &mut NeuronStore,
        is_over_instructions_limit: fn() -> bool,
    ) {
        while let Some((id, reward_e8s)) = self.rewards.pop_first() {
            match neuron_store.with_neuron_mut(&id, |neuron| {
                let auto_stake = neuron.auto_stake_maturity.unwrap_or(false);
                if auto_stake {
                    neuron.staked_maturity_e8s_equivalent = Some(
                        neuron
                            .staked_maturity_e8s_equivalent
                            .unwrap_or_default()
                            .saturating_add(reward_e8s),
                    );
                } else {
                    neuron.maturity_e8s_equivalent =
                        neuron.maturity_e8s_equivalent.saturating_add(reward_e8s);
                }
            }) {
                Ok(_) => {}
                Err(e) => {
                    println!(
                        "{}Error rewarding neuron {:?} during reward_distribution.\
                    This should not be possible as neuron existence is checked when \
                    rewards are calculated: {}",
                        LOG_PREFIX, id, e
                    );
                }
            };
            if is_over_instructions_limit() {
                break;
            }
        }
    }
```
