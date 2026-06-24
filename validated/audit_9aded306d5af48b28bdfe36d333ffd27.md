Audit Report

## Title
Unbounded Synchronous Loop Over All Voting Neurons in SNS Governance `distribute_rewards` Can Exhaust Instruction Limit, Permanently Bricking Reward Distribution - (File: rs/sns/governance/src/governance.rs)

## Summary

The SNS Governance `distribute_rewards` function contains two unbounded synchronous loops — one over all ballots of all settled proposals and one over all rewarded neurons — with no instruction-limit guard. A motivated attacker can inflate the ballot set (via many neurons and many settled proposals in a single reward period) until the IC instruction budget is exhausted on every heartbeat invocation, permanently freezing voting reward distribution. The NNS Governance canister has already fixed this exact pattern with a batched, instruction-aware timer task; SNS Governance has not.

## Finding Description

`distribute_rewards` in `rs/sns/governance/src/governance.rs` is called from the heartbeat (`run_periodic_tasks`) whenever a reward round is due. It executes two unbounded synchronous loops in a single message:

**Loop 1** — ballot aggregation across all settled proposals: [1](#0-0) 

**Loop 2** — maturity crediting for every rewarded neuron: [2](#0-1) 

A grep for `is_message_over_threshold` in `rs/sns/governance/src/governance.rs` returns zero matches, confirming there is no instruction-limit guard anywhere in this function. [3](#0-2) 

The worst-case instruction cost is `num_settled_proposals × max_number_of_neurons` for Loop 1, plus `max_number_of_neurons` for Loop 2. With `max_number_of_neurons` at 200,000 and even a modest number of proposals settled in the same reward period (e.g., 200 proposals × 200,000 ballots = 40 million iterations), the instruction budget of 40 billion can be approached or exceeded, especially as each HashMap entry access and Decimal arithmetic operation is non-trivial.

By contrast, NNS Governance was explicitly refactored to use a persistent, resumable `RewardsDistribution` state machine stored in stable memory, processed via a recurring timer task with an `is_message_over_threshold` guard after each neuron: [4](#0-3) [5](#0-4) 

The NNS timer task drains the pending distribution across multiple messages: [6](#0-5) 

## Impact Explanation

If `distribute_rewards` traps due to instruction exhaustion, the reward event is never recorded. Because `should_distribute_rewards()` will return true again on the next heartbeat (the round is still due), the same oversized ballot set triggers the same trap on every subsequent heartbeat. The result is permanent, irrecoverable freezing of all SNS voting rewards — no neuron ever receives maturity — until an SNS upgrade is deployed. This constitutes a **High** severity application/platform-level DoS with concrete, lasting harm to SNS governance token economics and all neuron holders.

## Likelihood Explanation

The attack path is fully unprivileged:
1. Any SNS token holder can stake tokens to create neurons up to `max_number_of_neurons`.
2. Neuron following (liquid democracy) allows a single vote to cascade to all follower neurons automatically, requiring no per-neuron action.
3. An attacker can submit proposals (paying the rejection fee) to inflate the number of settled proposals in a single reward period; rejected proposals still generate ballots for all voting neurons.
4. The reward round fires automatically via the heartbeat.

The cost is real (SNS tokens for neurons, proposal deposits) but not prohibitive for a motivated attacker targeting a specific SNS. Likelihood is **medium** today with heap-stored neurons and is **high** if SNS neurons are ever migrated to stable memory (where each access costs orders of magnitude more instructions, making exhaustion trivial at far smaller neuron counts).

## Recommendation

Apply the same fix already deployed in NNS Governance:

1. Extract the per-neuron maturity crediting into a persistent, resumable state machine stored in stable memory (analogous to `RewardsDistributionStateMachine` in `rs/nns/governance/src/reward/distribution.rs`).
2. Schedule a recurring timer task (analogous to `DistributeRewardsTask`) to drain the pending distribution across multiple messages.
3. In each message, check `is_message_over_threshold` after crediting each neuron and break when the limit is approached, resuming in the next timer invocation.
4. Record the reward event only after the distribution state machine reports completion.

## Proof of Concept

**Setup:**
- Deploy an SNS with `max_number_of_neurons` set to a large value (e.g., 100,000).
- Create 100,000 neurons via staking; configure following so a single vote cascades to all.
- Submit and allow settlement of a large number of proposals within a single reward period (e.g., by submitting proposals that get rejected, each generating 100,000 ballots).

**Trigger:**
- Wait for the next reward round. The heartbeat calls `run_periodic_tasks` → `distribute_rewards`.
- Loop 1 iterates over `num_proposals × 100,000` ballots; Loop 2 iterates over 100,000 neurons — all synchronously in one message.
- If the instruction budget is exhausted, the heartbeat traps.

**Observe:**
- `latest_reward_event` is never updated.
- No neuron receives maturity.
- Every subsequent heartbeat traps identically.
- The SNS reward economy is permanently frozen until an upgrade is deployed.

The root cause is confirmed at: [7](#0-6) [2](#0-1) 

with the absence of any `is_message_over_threshold` guard, in contrast to the NNS fix at: [8](#0-7)

### Citations

**File:** rs/sns/governance/src/governance.rs (L5763-5763)
```rust
    fn distribute_rewards(&mut self, supply: Tokens) {
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

**File:** rs/nns/governance/src/reward/distribution.rs (L1-17)
```rust
use crate::governance::{Governance, LOG_PREFIX};
use crate::neuron_store::NeuronStore;
use crate::pb::v1::RewardsDistributionInProgress;
use crate::storage::with_rewards_distribution_state_machine_mut;
#[cfg(not(feature = "canbench-rs"))]
use crate::timer_tasks::run_distribute_rewards_periodic_task;
use ic_cdk::println;
use ic_nervous_system_long_message::is_message_over_threshold;
use ic_nns_common::pb::v1::NeuronId;
use ic_stable_structures::storable::Bound;
use ic_stable_structures::{StableBTreeMap, Storable};
use prost::Message;
use std::borrow::Cow;
use std::collections::BTreeMap;

const BILLION: u64 = 1_000_000_000;
const DISTRIBUTION_MESSAGE_LIMIT: u64 = BILLION;
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

**File:** rs/nns/governance/src/reward/distribution.rs (L154-187)
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
```
