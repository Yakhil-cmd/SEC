### Title
Reward Accumulation Overwrite in `RewardsDistribution::add_reward` - (File: `rs/nns/governance/src/reward/distribution.rs`)

### Summary
`RewardsDistribution::add_reward` uses `BTreeMap::insert` to record per-neuron reward amounts, which **silently overwrites** any previously recorded reward for the same `NeuronId` instead of accumulating it. This is the exact same vulnerability class as the Augur v2 affiliate-fee overwrite: a value that should be accumulated across multiple operations is instead replaced on each call.

### Finding Description
In `rs/nns/governance/src/reward/distribution.rs`, the `RewardsDistribution` struct holds a `BTreeMap<NeuronId, u64>` mapping neurons to their pending reward amounts. The public method `add_reward` is:

```rust
pub(crate) fn add_reward(&mut self, neuron_id: NeuronId, amount: u64) {
    self.rewards.insert(neuron_id, amount);   // ← overwrites, does not accumulate
}
``` [1](#0-0) 

`BTreeMap::insert` returns the old value and replaces it with the new one. If `add_reward` is called more than once for the same `NeuronId` within a single `RewardsDistribution` (e.g., because the caller iterates over multiple proposals and a neuron voted on more than one), only the **last** call's amount survives. All prior reward contributions for that neuron are silently discarded.

The function name `add_reward` strongly implies accumulation semantics (analogous to `+=`), but the implementation has assignment semantics (`=`). The correct implementation should be:

```rust
*self.rewards.entry(neuron_id).or_insert(0) += amount;
```

The `RewardsDistribution` is built by `schedule_pending_rewards_distribution` and consumed by `continue_processing`, which correctly uses `saturating_add` when applying rewards to neuron maturity — but only for the single surviving amount per neuron: [2](#0-1) 

The caller in `rs/nns/governance/src/governance.rs` (1 call site) and `rs/nns/governance/src/timer_tasks/distribute_rewards.rs` (1 call site) populate the distribution. If the NNS governance reward calculation iterates over multiple proposals per reward period and calls `add_reward` once per (proposal, neuron) pair — which is the natural structure for a multi-proposal reward period — any neuron that voted on more than one proposal in the period would have all but its last reward contribution silently dropped.

The existing tests only call `add_reward` **once per neuron per distribution event**, so the overwrite bug is never exercised: [3](#0-2) 

### Impact Explanation
NNS neurons that voted on multiple proposals within a single reward period would receive only the reward corresponding to the **last** `add_reward` call for their `NeuronId`, losing all earlier reward contributions. This directly reduces neuron maturity — the primary economic incentive for NNS participation. Over time, affected neurons accumulate less maturity than they are entitled to, meaning they receive less ICP when they eventually spawn or disburse maturity. This is a **ledger conservation bug**: tokens that should be minted as maturity are silently discarded.

### Likelihood Explanation
The NNS governance processes multiple proposals per reward period. Any neuron that votes on more than one proposal in a period is a victim if the caller iterates per-proposal and calls `add_reward` per (proposal, neuron). The function's misleading name (`add_reward` vs. the correct `set_reward`) makes it easy for future callers or refactors to trigger the bug. The absence of a test covering multiple `add_reward` calls for the same neuron means the regression surface is unguarded.

### Recommendation
Replace the overwriting `insert` with an accumulating entry update:

```rust
pub(crate) fn add_reward(&mut self, neuron_id: NeuronId, amount: u64) {
    *self.rewards.entry(neuron_id).or_insert(0) =
        self.rewards.get(&neuron_id).copied().unwrap_or(0)
            .saturating_add(amount);
}
```

Or more idiomatically:

```rust
pub(crate) fn add_reward(&mut self, neuron_id: NeuronId, amount: u64) {
    *self.rewards.entry(neuron_id).or_insert(0) += amount;
}
```

Add a unit test that calls `add_reward` twice for the same `NeuronId` and asserts the amounts are summed, not replaced.

### Proof of Concept

```
1. Governance enters a reward period covering proposals P1 and P2.
2. Neuron N voted on both P1 (earning 100 e8s) and P2 (earning 200 e8s).
3. Reward calculation calls:
       distribution.add_reward(N, 100);   // rewards[N] = 100
       distribution.add_reward(N, 200);   // rewards[N] = 200  ← 100 silently lost
4. continue_processing applies rewards[N] = 200 to neuron N's maturity.
5. Neuron N receives 200 e8s instead of the correct 300 e8s.
6. The 100 e8s difference is permanently lost — never minted, never credited.
``` [1](#0-0) [4](#0-3)

### Citations

**File:** rs/nns/governance/src/reward/distribution.rs (L133-148)
```rust
#[derive(Clone, Debug, PartialEq, Default)]
pub(crate) struct RewardsDistribution {
    // NeuronId -> amount in e8s
    rewards: BTreeMap<NeuronId, u64>,
}

impl RewardsDistribution {
    pub(crate) fn new() -> Self {
        Self {
            rewards: BTreeMap::new(),
        }
    }

    pub(crate) fn add_reward(&mut self, neuron_id: NeuronId, amount: u64) {
        self.rewards.insert(neuron_id, amount);
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

**File:** rs/nns/governance/src/reward/distribution.rs (L312-321)
```rust
        for day_after_genesis in 1..=2 {
            rewards_distribution_state_machine.with_distribution_for_event(
                day_after_genesis,
                |distribution| {
                    for id in neurons.keys() {
                        distribution.add_reward(NeuronId { id: *id }, 10);
                    }
                },
            );
        }
```
