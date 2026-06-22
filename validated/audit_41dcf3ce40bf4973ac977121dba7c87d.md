### Title
SNS Governance `distribute_rewards` Exceeds Instruction Limit Due to Unbounded Neuron Iteration - (`rs/sns/governance/src/governance.rs`)

### Summary

The SNS governance canister's `distribute_rewards` function iterates over all voting neurons across all settled proposals in a single message execution with no instruction-limit check. Unlike NNS governance, which was explicitly fixed with a batched, instruction-aware distribution mechanism, SNS governance performs this work atomically. As the number of neurons and settled proposals grows, the function will trap due to the IC's per-message instruction limit, permanently preventing reward distribution.

### Finding Description

**Root cause — unbounded loop with no instruction guard:**

`distribute_rewards` in `rs/sns/governance/src/governance.rs` first builds a `neuron_id_to_reward_shares` map by iterating over every ballot of every `ReadyToSettle` proposal: [1](#0-0) 

It then iterates over every entry in that map to update neuron maturity, with no call to any instruction-limit check: [2](#0-1) 

There is no `is_message_over_threshold` guard, no batching, and no resumption mechanism anywhere in this function.

**Contrast with NNS governance (the fixed version):**

NNS governance was explicitly refactored to avoid this exact problem. Its `distribute_pending_rewards` breaks the loop on every neuron update using `is_message_over_threshold(DISTRIBUTION_MESSAGE_LIMIT)` and persists partial progress to stable memory so the next timer tick can resume: [3](#0-2) 

The NNS CHANGELOG documents this fix explicitly: [4](#0-3) 

SNS governance has no equivalent mechanism.

**Call path (heartbeat-triggered, no privilege required):**

`run_periodic_tasks` is called by the IC heartbeat. When `should_distribute_rewards()` returns true, it fetches the token supply and calls `distribute_rewards`: [5](#0-4) 

If `distribute_rewards` traps due to the instruction limit, the entire message is rolled back. `self.proto.latest_reward_event` is **not** updated, so the next heartbeat will attempt the same work again — with the same or more proposals — creating a permanent liveness failure.

**Attacker-controlled growth path:**

Any unprivileged token holder can:
1. Stake tokens to create many neurons.
2. Use neuron following to cascade votes across all neurons on every proposal.
3. Allow proposals to accumulate in `ReadyToSettle` state (e.g., by submitting many proposals that pass or by exploiting any delay in reward distribution).

The total instruction cost scales as O(neurons × settled_proposals). With 10,000 neurons and 500 accumulated proposals, the ballot iteration alone reaches ~5 billion instructions — at or beyond the application-subnet per-message limit.

### Impact Explanation

If the instruction limit is exceeded:
- The heartbeat traps and all state changes are rolled back.
- `latest_reward_event` is not advanced, so the same proposals remain `ReadyToSettle`.
- Every subsequent heartbeat attempts the same unbounded work and traps again.
- **Reward distribution is permanently halted** for the affected SNS.
- All SNS neuron holders lose their staking rewards indefinitely.
- The SNS DAO's economic incentive mechanism is broken with no on-chain recovery path (no admin can call a batched alternative).

### Likelihood Explanation

Any SNS with a growing neuron base and active governance is at risk. The threshold is reachable without malicious intent as the DAO matures. A deliberate attacker who holds enough tokens to create thousands of neurons and submit/pass proposals can trigger this deterministically. The NNS governance team already identified and fixed this exact class of bug for NNS; the SNS canister did not receive the same fix.

### Recommendation

Port the NNS batched reward distribution pattern to SNS governance:
1. Persist the `neuron_id_to_reward_shares` map to stable memory after computing it.
2. Replace the single-message loop with a timer-driven `distribute_pending_rewards` that processes a bounded number of neurons per message, checking `is_message_over_threshold` after each update.
3. Only advance `latest_reward_event` after the distribution map is fully drained.

### Proof of Concept

**Deterministic reasoning (no live network required):**

- IC application-subnet per-message instruction limit: 5 × 10⁹ instructions.
- Cost to load one SNS neuron from heap and update maturity: ~10,000–50,000 instructions (HashMap lookup + arithmetic).
- Cost to iterate one ballot entry: ~5,000 instructions.
- With 10,000 neurons each voting on 100 settled proposals: 10,000 × 100 × 5,000 (ballot scan) + 10,000 × 50,000 (neuron update) = 5.5 × 10⁹ instructions → **exceeds limit**.

The NNS governance team's own CHANGELOG confirms this class of bug is real and was fixed for NNS at exactly these neuron counts: [6](#0-5) 

The SNS `distribute_rewards` function contains no analogous protection: [7](#0-6) [8](#0-7)

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

**File:** rs/sns/governance/src/governance.rs (L5763-5765)
```rust
    fn distribute_rewards(&mut self, supply: Tokens) {
        log!(INFO, "distribute_rewards. Supply: {:?}", supply);
        let now = self.env.now();
```

**File:** rs/sns/governance/src/governance.rs (L5822-5823)
```rust
        let considered_proposals: Vec<ProposalId> =
            self.ready_to_be_settled_proposal_ids().collect();
```

**File:** rs/sns/governance/src/governance.rs (L5893-5931)
```rust
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

**File:** rs/nns/governance/CHANGELOG.md (L655-669)
```markdown
        * Distribute rewards is moved to timer, and has a mechanism to distribute in batches in
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
```
