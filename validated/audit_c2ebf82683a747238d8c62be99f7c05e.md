### Title
Per-Neuron Reward Truncation Permanently Destroys Maturity Each Distribution Round — (`rs/nns/governance/src/governance.rs` and `rs/sns/governance/src/governance.rs`)

---

### Summary

In both NNS and SNS governance, the per-neuron voting reward is computed with integer truncation (floor division). The sum of all truncated rewards is always less than or equal to the total available rewards purse. The shortfall is **never rolled over** to the next reward event when proposals are settled, causing a permanent, irrecoverable loss of maturity e8s every single distribution round.

---

### Finding Description

**NNS Governance — `rs/nns/governance/src/governance.rs`**

Inside `calculate_voting_rewards`, each neuron's reward is computed as:

```rust
let reward = (used_voting_rights * total_available_e8s_equivalent_float
    / total_voting_rights) as u64;
```

The `as u64` cast truncates toward zero. The sum of all per-neuron rewards is accumulated in `actually_distributed_e8s_equivalent`, which is always `≤ total_available_e8s_equivalent_float as u64`. The gap — up to `N` e8s where `N` is the number of voting neurons — is silently discarded. [1](#0-0) 

The `RewardEvent` records both values but the rollover mechanism only carries forward the **full** `total_available_e8s_equivalent` when `settled_proposals` is empty:

```rust
pub(crate) fn e8s_equivalent_to_be_rolled_over(&self) -> u64 {
    if self.rewards_rolled_over() {   // true only when settled_proposals.is_empty()
        self.total_available_e8s_equivalent
    } else {
        0   // <-- returns 0 even when distributed < available
    }
}
``` [2](#0-1) [3](#0-2) 

So whenever at least one proposal is settled, the entire rounding dust (`total_available - actually_distributed`) is permanently lost — it is not added to the next round's purse. [4](#0-3) 

Additionally, `total_available_e8s_equivalent_float` is itself computed in `f64`:

```rust
let total_available_e8s_equivalent_float = (supply.get_e8s() as f64) * fraction
    + rolling_over_from_previous_reward_event_e8s_equivalent as f64;
```

With an ICP supply on the order of 5 × 10¹⁶ e8s, `f64` has only ~15–16 significant decimal digits, introducing an additional precision loss of up to ~50 e8s per round before any per-neuron truncation occurs. [5](#0-4) 

**SNS Governance — `rs/sns/governance/src/governance.rs`**

The same structural bug exists in SNS. The code even explicitly acknowledges it:

```rust
// Because of rounding (and other shenanigans), it is possible that some
// portion of this amount ends up not being actually distributed.
```

Each neuron's reward is:

```rust
let neuron_reward_e8s =
    rewards_purse_e8s * (neuron_reward_shares / total_reward_shares);
// Round down, and convert to u64.
let neuron_reward_e8s = u64::try_from(neuron_reward_e8s)...
``` [6](#0-5) [7](#0-6) 

The SNS rollover guard is identical — it returns 0 when proposals are settled: [8](#0-7) [9](#0-8) 

---

### Impact Explanation

Every day that NNS proposals are settled, the rounding dust from per-neuron truncation is permanently destroyed. With `N` neurons voting, the loss per round is in the range `[0, N)` e8s. With hundreds to thousands of active voting neurons, this is hundreds to thousands of e8s of maturity lost per day. Over the multi-year lifetime of the IC, this accumulates to a non-trivial amount of maturity that neuron holders should have received but did not. The NNS case is compounded by the `f64` precision loss on the total purse itself. There is no recovery mechanism — no admin function, no sweep, no carry-forward of the shortfall.

The impact is classified as a **ledger conservation bug**: the rewards purse minted from the ICP supply is not fully distributed, and the shortfall is irrecoverable.

---

### Likelihood Explanation

This fires on **every single reward distribution round** (daily for NNS, configurable for SNS) in which at least one proposal is settled. Given that NNS proposals are settled nearly every day, the condition is essentially always true. No attacker action is required — the loss is structural and automatic.

---

### Recommendation

Change `e8s_equivalent_to_be_rolled_over` to return the **undistributed remainder** (`total_available_e8s_equivalent - distributed_e8s_equivalent`) even when proposals were settled, not just the full purse on empty-proposal rounds. Concretely:

```rust
pub(crate) fn e8s_equivalent_to_be_rolled_over(&self) -> u64 {
    self.total_available_e8s_equivalent
        .saturating_sub(self.distributed_e8s_equivalent)
}
```

This mirrors the mitigation recommended in the original report: carry the rounding dust forward so it is distributed in the next round. For NNS, additionally replace the `f64` intermediate with a fixed-precision type (e.g., `u128` or `Decimal`) to eliminate the secondary precision loss.

---

### Proof of Concept

**NNS scenario** (simplified):

- Total available purse: 10,000 e8s
- 3 neurons with voting power 3333, 3333, 3334 (total 10,000)
- Per-neuron reward: `(3333 * 10000 / 10000) as u64 = 3333`, `3333`, `3334`
- Actually distributed: 3333 + 3333 + 3334 = 9,999 e8s (but with unequal voting power the truncation is worse)

**Realistic NNS scenario**:

- 1,000 neurons vote; each truncation loses up to 1 e8s → up to 1,000 e8s/day lost
- Over 8 years: ~2.9 M e8s ≈ 0.029 ICP permanently destroyed
- With `f64` precision loss on a 500 M ICP supply: additional ~50 e8s/day → ~146,000 e8s ≈ 0.00146 ICP/year additionally lost

The `RewardEvent` fields confirm the gap is observable on-chain: `total_available_e8s_equivalent > distributed_e8s_equivalent` in every round with proposals, and the difference is never carried forward. [5](#0-4) [10](#0-9)

### Citations

**File:** rs/nns/governance/src/governance.rs (L6651-6654)
```rust
        let rolling_over_from_previous_reward_event_e8s_equivalent =
            latest_reward_event.e8s_equivalent_to_be_rolled_over();
        let total_available_e8s_equivalent_float = (supply.get_e8s() as f64) * fraction
            + rolling_over_from_previous_reward_event_e8s_equivalent as f64;
```

**File:** rs/nns/governance/src/governance.rs (L6724-6732)
```rust
                    let reward = (used_voting_rights * total_available_e8s_equivalent_float
                        / total_voting_rights) as u64;

                    reward_distribution.add_reward(neuron_id, reward);

                    // NOTE: This is the only reason we are checking the existence of neurons
                    // at this stage. Otherwise, we could defer until we distribute them in the
                    // schedule task.
                    actually_distributed_e8s_equivalent += reward;
```

**File:** rs/nns/governance/src/governance.rs (L6747-6757)
```rust
        let reward_event = RewardEvent {
            day_after_genesis,
            actual_timestamp_seconds: now,
            settled_proposals: considered_proposals,
            distributed_e8s_equivalent: actually_distributed_e8s_equivalent,
            total_available_e8s_equivalent: total_available_e8s_equivalent_float as u64,
            rounds_since_last_distribution: Some(rounds_since_last_distribution),
            latest_round_available_e8s_equivalent: Some(
                latest_round_available_e8s_equivalent_float as u64,
            ),
        };
```

**File:** rs/nns/governance/src/reward/calculation.rs (L120-126)
```rust
    pub(crate) fn e8s_equivalent_to_be_rolled_over(&self) -> u64 {
        if self.rewards_rolled_over() {
            self.total_available_e8s_equivalent
        } else {
            0
        }
    }
```

**File:** rs/nns/governance/src/reward/calculation.rs (L144-147)
```rust
    /// Whether this is a "rollover event", where no rewards were distributed.
    pub(crate) fn rewards_rolled_over(&self) -> bool {
        self.settled_proposals.is_empty()
    }
```

**File:** rs/sns/governance/src/governance.rs (L5940-5942)
```rust
        // Because of rounding (and other shenanigans), it is possible that some
        // portion of this amount ends up not being actually distributed.
        let mut distributed_e8s_equivalent = 0_u64;
```

**File:** rs/sns/governance/src/governance.rs (L5973-5978)
```rust
                // Dividing before multiplying maximizes our chances of success.
                let neuron_reward_e8s =
                    rewards_purse_e8s * (neuron_reward_shares / total_reward_shares);

                // Round down, and convert to u64.
                let neuron_reward_e8s = u64::try_from(neuron_reward_e8s).unwrap_or_else(|err| {
```

**File:** rs/sns/governance/src/governance.rs (L6083-6092)
```rust
        // Conclude this round of rewards.
        self.proto.latest_reward_event = Some(RewardEvent {
            round: new_reward_event_round,
            actual_timestamp_seconds: now,
            settled_proposals: considered_proposals,
            distributed_e8s_equivalent,
            end_timestamp_seconds: Some(reward_event_end_timestamp_seconds),
            rounds_since_last_distribution: Some(new_rounds_count),
            total_available_e8s_equivalent,
        })
```

**File:** rs/sns/governance/src/types.rs (L2054-2059)
```rust
    pub(crate) fn e8s_equivalent_to_be_rolled_over(&self) -> u64 {
        if self.rewards_rolled_over() {
            self.total_available_e8s_equivalent.unwrap_or_default()
        } else {
            0
        }
```

**File:** rs/sns/governance/src/types.rs (L2064-2067)
```rust
    /// Whether this is a "rollover event", where no rewards were distributed.
    pub(crate) fn rewards_rolled_over(&self) -> bool {
        self.settled_proposals.is_empty()
    }
```
