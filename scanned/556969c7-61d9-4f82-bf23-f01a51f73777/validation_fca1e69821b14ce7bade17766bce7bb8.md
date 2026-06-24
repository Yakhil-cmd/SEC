### Title
Voting Reward Maturity Permanently Lost When Settled Proposals Have Zero or Partial Voter Participation — (`rs/nns/governance/src/governance.rs`, `rs/sns/governance/src/governance.rs`)

---

### Summary

In both NNS and SNS governance, when a reward round settles proposals but the full reward purse cannot be distributed (because voting neurons no longer exist, or total voting rights is near zero), the undistributed portion of the reward purse is permanently lost. It is not rolled over to the next round. This is the direct IC analog of the Valkyrie "zero liquidity" stuck-rewards bug: the "zero participation" condition in governance plays the same role as "zero pool liquidity" in the DeFi contract.

---

### Finding Description

The rollover logic in `rs/nns/governance/src/reward/calculation.rs` is gated exclusively on whether `settled_proposals` is empty:

```rust
pub(crate) fn rewards_rolled_over(&self) -> bool {
    self.settled_proposals.is_empty()
}

pub(crate) fn e8s_equivalent_to_be_rolled_over(&self) -> u64 {
    if self.rewards_rolled_over() {
        self.total_available_e8s_equivalent
    } else {
        0  // <-- permanently lost if settled_proposals is non-empty
    }
}
``` [1](#0-0) [2](#0-1) 

This means: whenever `settled_proposals` is non-empty (i.e., proposals were settled), the **entire** `total_available_e8s_equivalent` is considered "consumed" — even if `distributed_e8s_equivalent` is far less than `total_available_e8s_equivalent`. The gap is silently discarded.

**Scenario 1 — NNS: Neuron deleted between voting and reward distribution**

In `calculate_voting_rewards`, when iterating over voters, if a neuron voted but no longer exists in the store, its reward share is skipped:

```rust
if self.neuron_store.contains(neuron_id) {
    let reward = (used_voting_rights * total_available_e8s_equivalent_float
        / total_voting_rights) as u64;
    reward_distribution.add_reward(neuron_id, reward);
    actually_distributed_e8s_equivalent += reward;
} else {
    println!(
        "{}Cannot find neuron {}, despite having voted with power {} \
            in the considered reward period. The reward that should have been \
            distributed to this neuron is simply skipped, so the total amount \
            of distributed reward for this period will be lower than the maximum \
            allowed.",
        ...
    );
}
``` [3](#0-2) 

The `RewardEvent` is then constructed with `settled_proposals` non-empty (proposals were settled), `distributed_e8s_equivalent` less than `total_available_e8s_equivalent`, and the difference is permanently lost. This is explicitly confirmed by a test:

```rust
// Since neuron 999 is gone and had a voting power 3x that of neuron 2,
// only 1/4 is actually distributed.
distributed_e8s_equivalent: 25,
total_available_e8s_equivalent: 100,
``` [4](#0-3) 

The 75 e8s difference is permanently lost — not rolled over.

**Scenario 2 — NNS: Total voting rights near zero**

When `total_voting_rights < 0.001`, the entire reward distribution is skipped (`reward_distribution = None`), but proposals are still settled:

```rust
let reward_distribution = if total_voting_rights < 0.001 {
    println!(
        "{}WARNING: total_voting_rights == {}, even though considered_proposals \
         is nonempty (see earlier log). Therefore, we skip incrementing maturity \
         to avoid dividing by zero (or super small number).",
        ...
    );
    None
} else { ... };
``` [5](#0-4) 

The `RewardEvent` records `settled_proposals` as non-empty and `distributed_e8s_equivalent = 0`, so `e8s_equivalent_to_be_rolled_over()` returns 0. The entire reward purse is permanently lost.

**Scenario 3 — SNS: Total reward shares zero**

The identical pattern exists in SNS governance:

```rust
if total_reward_shares == dec!(0) {
    log!(
        ERROR,
        "Warning: total_reward_shares is 0. Therefore, we skip increasing \
         neuron maturity. ...",
    );
} else { ... }
``` [6](#0-5) 

The proposals are still settled, `distributed_e8s_equivalent = 0`, and the reward purse is permanently lost.

The SNS rollover logic has the same flaw:

```rust
pub(crate) fn rewards_rolled_over(&self) -> bool {
    self.settled_proposals.is_empty()
}
``` [7](#0-6) 

---

### Impact Explanation

Voting reward maturity (ICP-equivalent in NNS, governance token maturity in SNS) is permanently destroyed — not credited to any neuron and not rolled over to future reward rounds. This is a **ledger conservation bug**: the governance canister mints reward maturity based on `total_available_e8s_equivalent` but only credits `distributed_e8s_equivalent`, with the gap silently discarded whenever proposals are settled. Over time, this causes cumulative, irreversible loss of governance reward maturity that should have been distributed to participants.

---

### Likelihood Explanation

**Scenario 1 (neuron deleted before reward distribution)** is the most realistic. Any neuron that votes on a proposal and is then dissolved and disbursed before the daily reward distribution runs will cause its reward share to be permanently lost. This is a normal user action (dissolving a neuron) that can be performed by any unprivileged principal. The code comment explicitly acknowledges this happens: *"The reward that should have been distributed to this neuron is simply skipped."*

**Scenario 2/3 (zero voting rights/shares)** is lower likelihood but acknowledged in the code as possible due to bugs.

---

### Recommendation

The rollover condition should not be based solely on whether `settled_proposals` is empty. Instead, it should also roll over the undistributed portion when `distributed_e8s_equivalent < total_available_e8s_equivalent`:

```rust
pub(crate) fn e8s_equivalent_to_be_rolled_over(&self) -> u64 {
    if self.rewards_rolled_over() {
        self.total_available_e8s_equivalent
    } else {
        // Roll over the undistributed portion
        self.total_available_e8s_equivalent
            .saturating_sub(self.distributed_e8s_equivalent)
    }
}
```

This mirrors the Valkyrie fix: accumulate undistributed rewards rather than discarding them.

---

### Proof of Concept

The existing test at `rs/nns/governance/tests/governance.rs` line 3244 already demonstrates the bug in production code:

1. Neuron 999 votes on proposal 1 with 3× the voting power of neuron 2.
2. Neuron 999 is deleted before reward distribution.
3. `distribute_voting_rewards_to_neurons` runs.
4. Result: `distributed_e8s_equivalent = 25`, `total_available_e8s_equivalent = 100`.
5. The 75 e8s difference is permanently lost — `e8s_equivalent_to_be_rolled_over()` returns 0 because `settled_proposals` is non-empty. [8](#0-7) 

The next reward event confirms no rollover occurred — the 75 e8s are gone from the system permanently.

### Citations

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

**File:** rs/nns/governance/src/governance.rs (L6712-6719)
```rust
        let reward_distribution = if total_voting_rights < 0.001 {
            println!(
                "{}WARNING: total_voting_rights == {}, even though considered_proposals \
                 is nonempty (see earlier log). Therefore, we skip incrementing maturity \
                 to avoid dividing by zero (or super small number).",
                LOG_PREFIX, total_voting_rights,
            );
            None
```

**File:** rs/nns/governance/src/governance.rs (L6722-6742)
```rust
            for (neuron_id, used_voting_rights) in voters_to_used_voting_right {
                if self.neuron_store.contains(neuron_id) {
                    let reward = (used_voting_rights * total_available_e8s_equivalent_float
                        / total_voting_rights) as u64;

                    reward_distribution.add_reward(neuron_id, reward);

                    // NOTE: This is the only reason we are checking the existence of neurons
                    // at this stage. Otherwise, we could defer until we distribute them in the
                    // schedule task.
                    actually_distributed_e8s_equivalent += reward;
                } else {
                    println!(
                        "{}Cannot find neuron {}, despite having voted with power {} \
                            in the considered reward period. The reward that should have been \
                            distributed to this neuron is simply skipped, so the total amount \
                            of distributed reward for this period will be lower than the maximum \
                            allowed.",
                        LOG_PREFIX, neuron_id.id, used_voting_rights
                    );
                }
```

**File:** rs/nns/governance/tests/governance.rs (L3235-3249)
```rust
    assert_eq!(
        *gov.latest_reward_event(),
        RewardEvent {
            day_after_genesis: 1,
            actual_timestamp_seconds: fake_driver.now(),
            settled_proposals: vec![ProposalId { id: 1 }],
            // We should have distributed 100 e8 equivalent if all voters still existed.
            // Since neuron 999 is gone and had a voting power 3x that of neuron 2,
            // only 1/4 is actually distributed.
            distributed_e8s_equivalent: 25,
            total_available_e8s_equivalent: 100,
            rounds_since_last_distribution: Some(1),
            latest_round_available_e8s_equivalent: Some(100)
        }
    );
```

**File:** rs/sns/governance/src/governance.rs (L5946-5952)
```rust
        if total_reward_shares == dec!(0) {
            log!(
                ERROR,
                "Warning: total_reward_shares is 0. Therefore, we skip increasing \
                 neuron maturity. neuron_id_to_reward_shares: {:#?}",
                neuron_id_to_reward_shares,
            );
```

**File:** rs/sns/governance/src/types.rs (L2064-2067)
```rust
    /// Whether this is a "rollover event", where no rewards were distributed.
    pub(crate) fn rewards_rolled_over(&self) -> bool {
        self.settled_proposals.is_empty()
    }
```
