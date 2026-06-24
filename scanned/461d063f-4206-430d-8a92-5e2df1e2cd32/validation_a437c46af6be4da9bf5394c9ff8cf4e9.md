### Title
Unbounded Loop Over All Proposal Ballots and Neurons in `distribute_rewards` Causes Instruction-Limit DoS of SNS Reward Distribution - (File: `rs/sns/governance/src/governance.rs`)

---

### Summary

The SNS Governance canister's `distribute_rewards` function iterates over every `ReadyToSettle` proposal's full ballot map and every voting neuron in a single synchronous execution with no instruction-limit guard. As the number of SNS neurons and settled proposals grows, this function will exceed the IC's per-message instruction limit, causing the heartbeat to trap and permanently breaking reward distribution for the SNS.

---

### Finding Description

`distribute_rewards` in `rs/sns/governance/src/governance.rs` performs two nested unbounded loops inside a single synchronous call:

**Loop 1 — over all settled proposals × all ballots per proposal:**

```rust
for proposal_id in &considered_proposals {
    if let Some(proposal) = self.get_proposal_data(*proposal_id) {
        for (voter, ballot) in &proposal.ballots {
            // accumulate neuron_id_to_reward_shares
        }
    }
}
```

**Loop 2 — over all neurons that voted:**

```rust
for (neuron_id, neuron_reward_shares) in neuron_id_to_reward_shares {
    let neuron: &mut Neuron = match self.get_neuron_result_mut(&neuron_id) { ... };
    // mutate neuron maturity
}
```

Neither loop checks the IC instruction counter or breaks early. The function is called synchronously from `run_periodic_tasks`, which is invoked from the canister heartbeat. [1](#0-0) [2](#0-1) [3](#0-2) 

The `considered_proposals` list is collected from all proposals in `ReadyToSettle` state with no cap:

```rust
let considered_proposals: Vec<ProposalId> =
    self.ready_to_be_settled_proposal_ids().collect();
``` [4](#0-3) 

Each proposal's `ballots` field is a `HashMap<String, Ballot>` with one entry per neuron that was eligible to vote at proposal creation time. With many neurons and many settled proposals, the total work is O(proposals × neurons).

**Contrast with NNS Governance:** The NNS governance canister has already been refactored to address exactly this class of issue. It uses a batched `RewardsDistribution` state machine with an explicit `is_over_instructions_limit` guard that breaks the loop and resumes in the next timer tick:

```rust
fn continue_processing(
    &mut self,
    neuron_store: &mut NeuronStore,
    is_over_instructions_limit: fn() -> bool,
) {
    while let Some((id, reward_e8s)) = self.rewards.pop_first() {
        // ... update neuron ...
        if is_over_instructions_limit() {
            break;
        }
    }
}
``` [5](#0-4) 

SNS governance has no equivalent mechanism.

---

### Impact Explanation

When the instruction limit is exceeded inside a heartbeat execution, the IC traps the message and rolls back all state changes for that heartbeat invocation. The `distribute_rewards` function performs state mutations (neuron maturity updates) only after completing the full accumulation loop. If the function traps mid-execution, no rewards are distributed and the `latest_reward_event` is not updated. On the next heartbeat, `should_distribute_rewards` returns true again, `distribute_rewards` is called again, and it traps again — creating a permanent DoS loop where:

- Voting rewards are never distributed to SNS neuron holders.
- The SNS governance heartbeat is permanently degraded.
- The SNS cannot recover without an upgrade that either reduces state or adds batching.

The IC's per-message instruction limit for update calls (including heartbeats) is 40 billion instructions. [6](#0-5) 

---

### Likelihood Explanation

**Medium.** SNS DAOs can accumulate neurons organically as the project grows. An SNS with thousands of neurons and many proposals settling simultaneously (e.g., after a period of high governance activity or after a canister upgrade that delayed heartbeats) can reach the threshold naturally. A motivated attacker holding SNS tokens can accelerate this by submitting many proposals (paying the proposal fee) to maximize the number of ballots that must be processed in a single reward round. The attack requires only an unprivileged SNS neuron — no admin key or governance majority is needed.

---

### Recommendation

Apply the same batched-distribution pattern already used in NNS governance:

1. After computing `neuron_id_to_reward_shares`, persist it to stable storage (analogous to `RewardsDistributionStateMachine`).
2. In each heartbeat/timer tick, process a bounded chunk of neurons, checking `ic_cdk::api::instruction_counter()` after each mutation.
3. Resume from the saved cursor on the next tick until the distribution is complete.

Additionally, cap the number of proposals that can be in `ReadyToSettle` state simultaneously, or cap the number of ballots per proposal (already partially addressed by neuron limits in SNS parameters).

---

### Proof of Concept

1. Deploy an SNS with N neurons (e.g., N = 10,000, achievable via swap participation).
2. Submit M proposals in rapid succession (e.g., M = 50, each costing the proposal fee). Neurons auto-vote via following, so each proposal accumulates N ballots.
3. Wait for all proposals to pass their voting period and enter `ReadyToSettle` state.
4. On the next reward round, `distribute_rewards` is called. It must iterate over M × N ballot entries (50 × 10,000 = 500,000 entries) plus 10,000 neuron mutations. Each stable-memory neuron read/write costs thousands of instructions. Total instruction cost easily exceeds 40B.
5. The heartbeat traps. `latest_reward_event` is not updated. On the next heartbeat, the same proposals are still `ReadyToSettle`, and the cycle repeats indefinitely.
6. Reward distribution is permanently broken until the SNS is upgraded. [7](#0-6) [8](#0-7)

### Citations

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

**File:** rs/sns/governance/src/governance.rs (L5763-5764)
```rust
    fn distribute_rewards(&mut self, supply: Tokens) {
        log!(INFO, "distribute_rewards. Supply: {:?}", supply);
```

**File:** rs/sns/governance/src/governance.rs (L5822-5823)
```rust
        let considered_proposals: Vec<ProposalId> =
            self.ready_to_be_settled_proposal_ids().collect();
```

**File:** rs/sns/governance/src/governance.rs (L5892-5931)
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
```

**File:** rs/sns/governance/src/governance.rs (L5954-5997)
```rust
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

**File:** rs/config/src/subnet_config.rs (L36-36)
```rust
pub(crate) const MAX_INSTRUCTIONS_PER_MESSAGE: NumInstructions = NumInstructions::new(40 * B);
```
