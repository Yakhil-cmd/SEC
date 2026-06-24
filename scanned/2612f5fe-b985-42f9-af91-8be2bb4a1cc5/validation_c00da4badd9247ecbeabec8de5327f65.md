### Title
Unbounded Synchronous Loop Over All Voting Neurons in SNS Governance `distribute_rewards` Can Exhaust Instruction Limit, Permanently Bricking Reward Distribution - (File: rs/sns/governance/src/governance.rs)

---

### Summary

The SNS Governance canister's `distribute_rewards` function iterates over all neurons that voted on settled proposals in a single synchronous call with no instruction-limit guard. An unprivileged participant who creates many neurons and votes with all of them can grow this collection to the point where the function exhausts the IC instruction limit on every heartbeat invocation, permanently preventing reward distribution. The NNS Governance canister has already recognized and fixed this exact class of bug by moving to a batched, instruction-aware timer task; the SNS Governance canister has not.

---

### Finding Description

`SNS Governance::distribute_rewards` is called from `run_periodic_tasks` (the heartbeat) whenever a reward round is due. It first builds a `neuron_id_to_reward_shares` map by iterating over every ballot of every settled proposal, then iterates over that map to credit maturity to each neuron — all in one synchronous execution slice with no instruction-limit check:

```rust
// rs/sns/governance/src/governance.rs  lines 5894–5930
for proposal_id in &considered_proposals {
    if let Some(proposal) = self.get_proposal_data(*proposal_id) {
        for (voter, ballot) in &proposal.ballots {
            ...
            *neuron_id_to_reward_shares.entry(neuron_id).or_insert_with(|| dec!(0))
                += reward_shares;
        }
    }
}
```

```rust
// rs/sns/governance/src/governance.rs  lines 5954–5997
for (neuron_id, neuron_reward_shares) in neuron_id_to_reward_shares {
    let neuron: &mut Neuron = match self.get_neuron_result_mut(&neuron_id) { ... };
    ...
    neuron.maturity_e8s_equivalent += neuron_reward_e8s;
    ...
}
``` [1](#0-0) [2](#0-1) 

There is no call to `is_message_over_threshold`, no batching, and no early-exit mechanism. The entire distribution must complete within a single message execution or the heartbeat traps.

Compare this to the NNS Governance, which was explicitly fixed to use a batched, instruction-aware timer task:

```rust
// rs/nns/governance/src/reward/distribution.rs  lines 154–187
fn continue_processing(
    &mut self,
    neuron_store: &mut NeuronStore,
    is_over_instructions_limit: fn() -> bool,
) {
    while let Some((id, reward_e8s)) = self.rewards.pop_first() {
        ...
        if is_over_instructions_limit() {
            break;
        }
    }
}
``` [3](#0-2) 

The NNS Governance changelog explicitly documents this fix:

> "Unstaking maturity task has a limit of 100 neurons per message, which prevents it from exceeding instruction limit."
> "Avoid applying `approve_genesis_kyc` to an unbounded number of neurons, but at most 1000 neurons." [4](#0-3) 

The SNS Governance `distribute_rewards` has received no equivalent fix and remains a single unbounded synchronous loop.

---

### Impact Explanation

If the instruction limit is exhausted during `distribute_rewards`, the heartbeat traps and the reward event is never recorded. Because the function is called only when `should_distribute_rewards()` returns true (i.e., a new round is due), and because the same large neuron set will be present on every subsequent heartbeat, the trap recurs indefinitely. The result is:

- **Voting rewards are permanently frozen.** No neuron ever receives maturity for voting.
- **The reward event is never updated**, so `latest_reward_event` stays stale and the SNS governance token economy is broken.
- **No recovery path exists** without an SNS upgrade that either reduces the neuron count or patches the function to be batched.

This is the direct IC analog of H-7: a critical protocol function is bricked by an unbounded loop that an attacker can inflate.

---

### Likelihood Explanation

The attacker path is fully unprivileged:

1. **Create many neurons.** Any token holder can stake SNS tokens to create neurons. The maximum is governed by `max_number_of_neurons` in `NervousSystemParameters`, which is configurable per SNS and can be set to values in the tens of thousands or higher.
2. **Vote with all neurons on every proposal.** Neurons can follow other neurons (liquid democracy), so a single vote can cascade to all follower neurons automatically, requiring no per-neuron action.
3. **Wait for a reward round.** `distribute_rewards` is called automatically by the heartbeat.

The IC instruction limit for a single message on an application subnet is 40 billion instructions. Each iteration of the reward loop accesses a heap `HashMap` entry and performs arithmetic — roughly 1,000–5,000 instructions per neuron. At 200,000 neurons (a plausible `max_number_of_neurons`), the loop consumes on the order of 200 million–1 billion instructions, which is within the limit. However:

- If neurons are ever migrated to stable memory in SNS (as NNS is doing), each access costs orders of magnitude more instructions, making exhaustion trivial.
- Multiple settled proposals in the same round multiply the ballot-iteration cost proportionally.
- The NNS Governance team already treated this pattern as a real risk at far smaller neuron counts (100–1,000 neurons per message), which is why they introduced the batched approach.

Likelihood is **medium** today (requires a large SNS with many neurons and multiple settled proposals) and **high** if SNS neurons are ever moved to stable memory.

---

### Recommendation

Apply the same fix already used in NNS Governance:

1. Move reward distribution out of the synchronous `distribute_rewards` call and into a persistent, resumable state machine stored in stable memory.
2. Process neurons in batches, checking `is_message_over_threshold` after each neuron and breaking when the limit is approached.
3. Schedule a recurring timer task (analogous to `DistributeRewardsTask` in NNS) to drain the pending distribution across multiple messages.

Concretely, mirror the NNS pattern:

```rust
// NNS pattern — apply analogously to SNS Governance
const DISTRIBUTION_MESSAGE_LIMIT: u64 = 1_000_000_000; // 1 billion instructions

fn continue_processing(&mut self, neuron_store: &mut NeuronStore,
                       is_over: fn() -> bool) {
    while let Some((id, reward_e8s)) = self.rewards.pop_first() {
        // credit neuron ...
        if is_over() { break; }
    }
}
``` [5](#0-4) [6](#0-5) 

---

### Proof of Concept

**Setup:**
- Deploy an SNS with `max_number_of_neurons` set to a large value (e.g., 100,000).
- Create 100,000 neurons by staking SNS tokens from many accounts (or use neuron following so a single vote cascades to all).
- Submit and pass a governance proposal so it enters `ReadyToSettle`.

**Trigger:**
- Wait for the next reward round. The heartbeat calls `run_periodic_tasks` → `distribute_rewards`.
- `distribute_rewards` builds `neuron_id_to_reward_shares` with 100,000 entries and then iterates over all of them synchronously.
- If the instruction budget is exhausted, the heartbeat traps.

**Observe:**
- `latest_reward_event` is never updated.
- No neuron receives maturity.
- Every subsequent heartbeat traps in the same way.
- The SNS reward economy is permanently frozen until an upgrade is deployed.

The root cause is confirmed at: [7](#0-6) [2](#0-1) 

with the absence of any `is_message_over_threshold` guard, in contrast to the NNS fix at: [8](#0-7)

### Citations

**File:** rs/sns/governance/src/governance.rs (L5763-5763)
```rust
    fn distribute_rewards(&mut self, supply: Tokens) {
```

**File:** rs/sns/governance/src/governance.rs (L5894-5930)
```rust
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

**File:** rs/nns/governance/src/reward/distribution.rs (L1-52)
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

impl Governance {
    pub(crate) fn schedule_pending_rewards_distribution(
        &self,
        day_after_genesis: u64,
        distribution: RewardsDistribution,
    ) {
        let result =
            with_rewards_distribution_state_machine_mut(|rewards_distribution_state_machine| {
                rewards_distribution_state_machine
                    .add_rewards_distribution(day_after_genesis, distribution)
            });

        if let Err(e) = result {
            println!("{}Error scheduling rewards distribution: {}", LOG_PREFIX, e);
        }

        // TODO(NNS1-3643) Determine if there is a way we can refactor this so that
        // canbench can call timer setting function stubs (or even immediately execute the work)
        #[cfg(not(feature = "canbench-rs"))]
        run_distribute_rewards_periodic_task();
    }

    // Returns if there is work left to do
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

**File:** rs/nns/governance/CHANGELOG.md (L655-675)
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
    * This will allow rewards to be compatible with neurons being stored in Stable Memory.
* Ramp up the failure rate of _pb method to 0.7 again.

## Fixed

* Avoid applying `approve_genesis_kyc` to an unbounded number of neurons, but at most 1000 neurons.
```

**File:** rs/nns/governance/src/timer_tasks/distribute_rewards.rs (L43-55)
```rust
impl PeriodicSyncTask for DistributeRewardsTask {
    fn execute(self) {
        self.governance.with_borrow_mut(|governance| {
            let work_left = governance.distribute_pending_rewards();
            if !work_left {
                cancel_distribute_pending_rewards_timer();
            }
        });
    }

    const NAME: &'static str = "distribute_rewards";
    const INTERVAL: Duration = Duration::from_secs(2);
}
```
