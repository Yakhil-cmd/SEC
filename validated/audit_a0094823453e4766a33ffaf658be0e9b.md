### Title
Stateless Periodic Batch Cap in `UnstakeMaturityOfDissolvedNeuronsTask` Causes Permanent Staking-Maturity Delay for Neurons Beyond the 100-Neuron Window - (File: rs/nns/governance/src/timer_tasks/unstake_maturity_of_dissolved_neurons.rs)

---

### Summary

The NNS Governance canister's `UnstakeMaturityOfDissolvedNeuronsTask` runs every 60 seconds and processes at most 100 dissolved neurons per invocation. Because the task is implemented as a stateless `PeriodicSyncTask` with no cursor, it always scans from the beginning of the active-neuron iterator and takes the first 100 qualifying neurons. If more than 100 neurons are simultaneously dissolved and have staked maturity, the neurons beyond the first 100 (ordered by neuron ID) are never processed in that invocation and must wait for the next 60-second tick. Under sustained high dissolved-neuron counts, neurons with higher IDs may experience indefinitely delayed maturity unstaking.

---

### Finding Description

`UnstakeMaturityOfDissolvedNeuronsTask` is a `PeriodicSyncTask` that fires every 60 seconds: [1](#0-0) 

Each invocation calls `Governance::unstake_maturity_of_dissolved_neurons`, which hard-codes a cap of 100 neurons: [2](#0-1) 

The cap is enforced by `list_neurons_ready_to_unstake_maturity`, which iterates the active-neuron store in neuron-ID order and takes the first `max_num_neurons` qualifying entries: [3](#0-2) 

Because `UnstakeMaturityOfDissolvedNeuronsTask` is a `PeriodicSyncTask` (not a `RecurringSyncTask`), it carries **no cursor state** between invocations: [4](#0-3) 

Contrast this with `PruneFollowingTask`, which is a `RecurringSyncTask` that explicitly stores and advances a `begin: Bound<NeuronId>` cursor across invocations so every neuron is eventually visited: [5](#0-4) 

The `unstake_maturity_of_dissolved_neurons` task has no equivalent cursor. Every 60-second tick restarts the scan from the lowest neuron ID. If there are, say, 500 dissolved neurons with staked maturity, only the 100 with the lowest IDs are processed each tick. The remaining 400 are processed in subsequent ticks (4 more minutes), but only if the first 100 have been cleared. If new neurons continuously dissolve and enter the eligible set at low IDs, neurons with high IDs may be perpetually starved.

---

### Impact Explanation

Staked maturity that should be converted to regular maturity upon dissolution is delayed. Regular maturity is subject to maturity modulation and can be spawned into new neurons or disbursed. Staked maturity cannot be disbursed or spawned directly. A neuron owner whose neuron has dissolved but whose staked maturity has not been unstaked is denied access to their economic rewards for the duration of the delay. In a scenario with a large number of simultaneously dissolved neurons (e.g., a mass-dissolution event at the end of a popular dissolve-delay period), neurons with higher IDs could experience delays of many minutes to hours before their maturity is correctly moved. This constitutes a governance-layer resource-accounting bug: the canister's periodic task does not guarantee timely processing of all eligible neurons within a bounded number of intervals.

---

### Likelihood Explanation

The NNS currently has hundreds of thousands of neurons. Mass-dissolution events are realistic (e.g., neurons created with the same dissolve delay all dissolving simultaneously). The 100-neuron cap per 60-second tick means that if even 101 neurons dissolve simultaneously with staked maturity, at least one neuron will not be processed in the first tick. The likelihood of this occurring at scale is moderate to high given the size of the NNS neuron population.

---

### Recommendation

Convert `UnstakeMaturityOfDissolvedNeuronsTask` from a `PeriodicSyncTask` to a `RecurringSyncTask` that carries a `begin: Bound<NeuronId>` cursor (identical to the pattern used by `PruneFollowingTask`). After each batch of 100 neurons is processed, the cursor advances to the last processed neuron ID. When the cursor reaches the end of the store, it resets to `Bound::Unbounded` for the next full pass. This guarantees that every dissolved neuron with staked maturity is eventually processed regardless of how many are eligible simultaneously.

---

### Proof of Concept

1. Create 200 neurons, all with `auto_stake_maturity = true` and the same dissolve delay.
2. Start dissolving all neurons simultaneously and advance time past the dissolve delay.
3. Observe that `unstake_maturity_of_dissolved_neurons` is called with `MAX_NEURONS_TO_UNSTAKE = 100`.
4. After one 60-second tick, only the 100 neurons with the lowest IDs have their staked maturity moved to regular maturity.
5. The remaining 100 neurons retain staked maturity and must wait for the next tick.

The stateless scan is confirmed at: [6](#0-5) 

The 60-second fixed interval with no cursor is confirmed at: [7](#0-6) 

The `PeriodicSyncTask` trait carries no state between invocations by design: [8](#0-7)

### Citations

**File:** rs/nns/governance/src/timer_tasks/unstake_maturity_of_dissolved_neurons.rs (L10-10)
```rust
const UNSTAKE_MATURITY_OF_DISSOLVED_NEURONS_INTERVAL: Duration = Duration::from_secs(60);
```

**File:** rs/nns/governance/src/timer_tasks/unstake_maturity_of_dissolved_neurons.rs (L23-31)
```rust
impl PeriodicSyncTask for UnstakeMaturityOfDissolvedNeuronsTask {
    fn execute(self) {
        self.governance.with_borrow_mut(|governance| {
            governance.unstake_maturity_of_dissolved_neurons();
        });
    }

    const NAME: &'static str = "unstake_maturity_of_dissolved_neurons";
    const INTERVAL: Duration = UNSTAKE_MATURITY_OF_DISSOLVED_NEURONS_INTERVAL;
```

**File:** rs/nns/governance/src/governance.rs (L6388-6398)
```rust
    pub fn unstake_maturity_of_dissolved_neurons(&mut self) {
        // We assume that modifying a neuron can use <400 StableBTreeMap read operations and <400
        // write operations (100 recent ballots + 270 followees entries + others), and one read + one
        // write operation takes 400K instructions in total, unstaking 100 neurons should take less
        // than 16B instructions. Note that this is the worst case scenario, and the actual number
        // of instructions should be much less.
        const MAX_NEURONS_TO_UNSTAKE: usize = 100;
        let now_seconds = self.env.now();
        self.neuron_store
            .unstake_maturity_of_dissolved_neurons(now_seconds, MAX_NEURONS_TO_UNSTAKE);
    }
```

**File:** rs/nns/governance/src/neuron_store.rs (L578-592)
```rust
    fn list_neurons_ready_to_unstake_maturity(
        &self,
        now_seconds: u64,
        max_num_neurons: usize,
    ) -> Vec<NeuronId> {
        self.with_active_neurons_iter_sections(
            |iter| {
                iter.filter(|neuron| neuron.ready_to_unstake_maturity(now_seconds))
                    .take(max_num_neurons)
                    .map(|neuron| neuron.id())
                    .collect()
            },
            NeuronSections::NONE,
        )
    }
```

**File:** rs/nervous_system/timer_task/src/lib.rs (L236-255)
```rust
pub trait PeriodicSyncTask: Copy + Sized + 'static {
    // TODO: can periodic tasks have a state that is mutable across invocations?
    fn execute(self);

    fn schedule(self, metrics_registry: MetricsRegistryRef) -> TimerId {
        set_timer_interval(Self::INTERVAL, move || async move {
            let instructions_before = instruction_counter();

            self.execute();

            let instructions_used = instruction_counter() - instructions_before;
            with_sync_metrics(metrics_registry, Self::NAME, |metrics| {
                metrics.record(instructions_used, now_seconds());
            });
        })
    }

    const NAME: &'static str;
    const INTERVAL: Duration;
}
```

**File:** rs/nns/governance/src/timer_tasks/prune_following.rs (L29-57)
```rust
pub(super) struct PruneFollowingTask {
    governance: &'static LocalKey<RefCell<Governance>>,
    begin: Bound<NeuronId>,
}

impl PruneFollowingTask {
    pub fn new(governance: &'static LocalKey<RefCell<Governance>>) -> Self {
        Self {
            governance,
            begin: Bound::Unbounded,
        }
    }
}

impl RecurringSyncTask for PruneFollowingTask {
    fn execute(self) -> (Duration, Self) {
        let new_begin = self.governance.with_borrow_mut(|governance| {
            let carry_on = || !is_message_over_threshold(MAX_PRUNE_SOME_FOLLOWING_INSTRUCTIONS);
            governance.prune_some_following(self.begin, carry_on)
        });

        (
            PRUNE_FOLLOWING_INTERVAL,
            Self {
                governance: self.governance,
                begin: new_begin,
            },
        )
    }
```
