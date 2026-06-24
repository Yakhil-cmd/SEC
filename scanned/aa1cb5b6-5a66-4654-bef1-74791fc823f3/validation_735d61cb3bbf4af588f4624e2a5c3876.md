The question's framing about voting activity / `start_reducing_voting_power_after_seconds` is wrong — those govern voting power reduction, not `is_inactive()`. But the core observation about `is_funded()` omitting `staked_maturity_e8s_equivalent` is correct and leads to a real bug. Here is the precise analysis:

---

### Title
Staked Maturity Permanently Locked in Inactive Dissolved Neuron — (`rs/nns/governance/src/neuron/types.rs`, `rs/nns/governance/src/neuron_store.rs`)

### Summary

`is_funded()` does not include `staked_maturity_e8s_equivalent`. A dissolved neuron with only staked maturity (no ICP stake, no unstaked maturity) that dissolved more than 2 weeks ago is classified as `is_inactive() == true`. `with_active_neurons_iter_sections` filters it out, so `list_neurons_ready_to_unstake_maturity` never returns it, and the timer task never unstakes its maturity — permanently.

---

### Finding Description

**`is_funded()` omits staked maturity:** [1](#0-0) 

```rust
pub fn is_funded(&self) -> bool {
    let amount_e8s = self
        .stake_e8s()
        .saturating_add(self.maturity_e8s_equivalent);
    amount_e8s > 0
}
```

Only `stake_e8s()` (i.e., `cached_neuron_stake_e8s − neuron_fees_e8s`) and `maturity_e8s_equivalent` are summed. `staked_maturity_e8s_equivalent` is absent.

**`is_inactive()` uses `is_funded()` as its "funded" gate:** [2](#0-1) 

A neuron is inactive when: not seed/ECT, **not funded**, dissolved ≥ 2 weeks ago, not Neurons' Fund member. Because `is_funded()` ignores staked maturity, a neuron with `staked_maturity_e8s_equivalent > 0` but `cached_neuron_stake_e8s == 0` and `maturity_e8s_equivalent == 0` passes the "not funded" gate and becomes inactive.

**`with_active_neurons_iter_sections` filters inactive neurons:** [3](#0-2) 

```rust
stable_store
    .range_neurons_sections(.., sections)
    .filter(|n| !n.is_inactive(now)),
```

**`list_neurons_ready_to_unstake_maturity` is built on top of this filtered iterator:** [4](#0-3) 

So the neuron is never returned, never passed to `unstake_maturity`, and its staked maturity is permanently stranded.

**The timer task runs every 60 seconds but can never reach the neuron:** [5](#0-4) 

---

### Impact Explanation

A neuron controller who disburses their ICP stake after dissolution (setting `cached_neuron_stake_e8s = 0`) while retaining `staked_maturity_e8s_equivalent > 0` will, after 2 weeks, have their staked maturity permanently locked. The timer task is the **only** mechanism to convert staked maturity to regular maturity; there is no user-callable governance method to trigger it manually. The controller permanently loses the ability to spawn that maturity into ICP.

---

### Likelihood Explanation

This is reachable by any neuron controller through normal governance flows (dissolve → disburse stake → wait 2 weeks). No privileged access is required. The controller harms only themselves, but the invariant stated in the comment — *"No neuron in stable storage should have staked maturity"* — is violated. [6](#0-5) 

---

### Recommendation

Include `staked_maturity_e8s_equivalent` in `is_funded()`:

```rust
pub fn is_funded(&self) -> bool {
    let amount_e8s = self
        .stake_e8s()
        .saturating_add(self.staked_maturity_e8s_equivalent.unwrap_or(0))
        .saturating_add(self.maturity_e8s_equivalent);
    amount_e8s > 0
}
```

This matches the intent expressed in the proptest at: [7](#0-6) 

which already computes `net_funding_e8s` including `staked_maturity_e8s_equivalent` and asserts it matches `is_inactive`.

---

### Proof of Concept

1. Create a neuron with `cached_neuron_stake_e8s = 0`, `maturity_e8s_equivalent = 0`, `staked_maturity_e8s_equivalent = 1_000_000`, dissolved at `now - 3 * 7 * ONE_DAY_SECONDS` (3 weeks ago).
2. Confirm `neuron.is_inactive(now) == true` (it will be, because `is_funded()` returns `false`).
3. Call `neuron_store.unstake_maturity_of_dissolved_neurons(now, usize::MAX)` repeatedly.
4. Assert `neuron.staked_maturity_e8s_equivalent == Some(1_000_000)` — it never changes.

### Citations

**File:** rs/nns/governance/src/neuron/types.rs (L1060-1098)
```rust
    pub fn is_inactive(&self, now: u64) -> bool {
        // Require condition 1.
        if self.is_seed_neuron() || self.is_ect_neuron() {
            return false;
        }

        // Require condition 2.
        if self.is_funded() {
            return false;
        }

        // Require condition 3.

        // 3.1: Interpret dissolve_state field.
        let dissolved_at_timestamp_seconds = match self.dissolved_at_timestamp_seconds() {
            // None -> not dissolving -> will be dissolved in the future -> not dissolved now ->
            // certainly was not dissolved sufficiently "long" ago!
            None => {
                return false;
            }
            Some(ok) => ok,
        };

        // 3.2: Now, we know when self is "dissolved" (could be in the past, present, or future).
        // Thus, we can evaluate whether that happened sufficiently long ago.
        let max_dissolved_at_timestamp_seconds_to_be_inactive =
            now.saturating_sub(2 * 7 * ONE_DAY_SECONDS);
        if dissolved_at_timestamp_seconds > max_dissolved_at_timestamp_seconds_to_be_inactive {
            return false;
        }

        // Finally, require condition 4: Member of the Neuron's Fund.
        if self.is_a_neurons_fund_member() {
            return false;
        }

        // All requirements have been met.
        true
    }
```

**File:** rs/nns/governance/src/neuron/types.rs (L1112-1117)
```rust
    pub fn is_funded(&self) -> bool {
        let amount_e8s = self
            .stake_e8s()
            .saturating_add(self.maturity_e8s_equivalent);
        amount_e8s > 0
    }
```

**File:** rs/nns/governance/src/neuron_store.rs (L519-533)
```rust
    fn with_active_neurons_iter_sections<R>(
        &self,
        callback: impl for<'b> FnOnce(Box<dyn Iterator<Item = Neuron> + 'b>) -> R,
        sections: NeuronSections,
    ) -> R {
        with_stable_neuron_store(|stable_store| {
            let now = self.now();
            let iter = Box::new(
                stable_store
                    .range_neurons_sections(.., sections)
                    .filter(|n| !n.is_inactive(now)),
            );
            callback(iter)
        })
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

**File:** rs/nns/governance/src/neuron_store.rs (L645-647)
```rust
        // Filter all the neurons that are currently in "dissolved" state and have some staked maturity.
        // No neuron in stable storage should have staked maturity.
        for neuron_id in neuron_ids {
```

**File:** rs/nns/governance/src/timer_tasks/unstake_maturity_of_dissolved_neurons.rs (L10-31)
```rust
const UNSTAKE_MATURITY_OF_DISSOLVED_NEURONS_INTERVAL: Duration = Duration::from_secs(60);

#[derive(Copy, Clone)]
pub(super) struct UnstakeMaturityOfDissolvedNeuronsTask {
    governance: &'static LocalKey<RefCell<Governance>>,
}

impl UnstakeMaturityOfDissolvedNeuronsTask {
    pub fn new(governance: &'static LocalKey<RefCell<Governance>>) -> Self {
        Self { governance }
    }
}

impl PeriodicSyncTask for UnstakeMaturityOfDissolvedNeuronsTask {
    fn execute(self) {
        self.governance.with_borrow_mut(|governance| {
            governance.unstake_maturity_of_dissolved_neurons();
        });
    }

    const NAME: &'static str = "unstake_maturity_of_dissolved_neurons";
    const INTERVAL: Duration = UNSTAKE_MATURITY_OF_DISSOLVED_NEURONS_INTERVAL;
```

**File:** rs/nns/governance/src/governance/tests/mod.rs (L1121-1128)
```rust
            let net_funding_e8s = (
                cached_neuron_stake_e8s
                    .saturating_sub(neuron_fees_e8s)
                    .saturating_add(staked_maturity_e8s_equivalent)
            )
            + maturity_e8s_equivalent;
            let is_funded = net_funding_e8s > 0;

```
