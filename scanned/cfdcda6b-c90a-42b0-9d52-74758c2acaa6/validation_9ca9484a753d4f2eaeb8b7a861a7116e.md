### Title
Voting Reward Residual Permanently Lost Due to Integer Truncation With No Rollover - (`rs/nns/governance/src/governance.rs`)

### Summary

In the NNS governance canister, per-neuron voting reward computation uses integer truncation (`as u64`) when dividing the total reward pool among voters. The sum of all truncated per-neuron rewards is always ≤ the total available reward pool. The residual (`total_available - sum(distributed)`) is never rolled over to the next reward round when proposals are settled, causing it to be permanently lost. This accumulates every reward round in which at least one proposal is settled.

### Finding Description

In `calculate_voting_rewards` in `rs/nns/governance/src/governance.rs`, the per-neuron reward is computed as:

```rust
let reward = (used_voting_rights * total_available_e8s_equivalent_float
    / total_voting_rights) as u64;  // floor truncation
```

The `as u64` cast truncates the fractional part. With N voters, up to N−1 e8s can be lost per round. The code itself acknowledges this:

> "Because of rounding (and other shenanigans), it is possible that some portion of this amount ends up not being actually distributed."

The rollover mechanism in `e8s_equivalent_to_be_rolled_over()` only returns the `total_available_e8s_equivalent` when `settled_proposals.is_empty()` (i.e., no proposals were settled). When proposals ARE settled, it returns 0:

```rust
pub(crate) fn e8s_equivalent_to_be_rolled_over(&self) -> u64 {
    if self.rewards_rolled_over() {
        self.total_available_e8s_equivalent
    } else {
        0  // residual is dropped
    }
}
pub(crate) fn rewards_rolled_over(&self) -> bool {
    self.settled_proposals.is_empty()
}
```

This means the difference `total_available_e8s_equivalent - distributed_e8s_equivalent` is permanently discarded every round in which proposals are settled. The same pattern exists in SNS governance's `distribute_rewards` in `rs/sns/governance/src/governance.rs` at the `u64::try_from(neuron_reward_e8s)` truncation step.

### Impact Explanation

**Severity: Low.** The residual per round is bounded by the number of voting neurons (at most N−1 e8s per round, where N is the number of distinct voters). However, this loss is permanent and accumulates every single reward round. Over years of operation with thousands of neurons voting daily, the cumulative lost maturity is non-trivial. The lost maturity is never minted, so it represents a permanent reduction in the effective reward rate for all stakers.

### Likelihood Explanation

**Likelihood: High.** This occurs every reward round in which at least one proposal is settled (i.e., `settled_proposals` is non-empty). In normal NNS operation, proposals are settled and rewards distributed daily. The residual loss is therefore guaranteed to occur on every active reward round, accumulating indefinitely.

### Recommendation

The residual `total_available_e8s_equivalent - distributed_e8s_equivalent` should be rolled over to the next reward event regardless of whether proposals were settled. The `e8s_equivalent_to_be_rolled_over` function should be changed to return the undistributed residual even when `settled_proposals` is non-empty:

```rust
pub(crate) fn e8s_equivalent_to_be_rolled_over(&self) -> u64 {
    // Always roll over the undistributed residual
    self.total_available_e8s_equivalent
        .saturating_sub(self.distributed_e8s_equivalent)
}
```

The same fix applies to SNS governance's `distribute_rewards`.

### Proof of Concept

The existing test at `rs/nns/governance/tests/governance.rs` already demonstrates the loss. When a neuron is deleted after voting, 75 out of 100 available e8s are lost and not rolled over:

```
distributed_e8s_equivalent: 25,
total_available_e8s_equivalent: 100,
```

The 75 e8s difference is silently dropped. Even without deleted neurons, with N voters each receiving `floor(share * total)`, the sum is always ≤ total, and the residual is dropped every round.

A minimal scenario: 3 neurons with equal voting power, total reward pool = 100 e8s. Each neuron receives `floor(100/3) = 33` e8s. Total distributed = 99. The 1 e8s residual is permanently lost and not rolled over to the next round.

---

**Root cause files:**

- `rs/nns/governance/src/governance.rs` lines 6724–6725 (truncation) [1](#0-0) 
- `rs/nns/governance/src/reward/calculation.rs` lines 120–126 (rollover returns 0 when proposals settled) [2](#0-1) 
- `rs/nns/governance/src/reward/calculation.rs` lines 144–147 (rollover condition) [3](#0-2) 
- `rs/sns/governance/src/governance.rs` lines 5974–5978 (SNS truncation) [4](#0-3) 
- `rs/sns/governance/src/types.rs` lines 2054–2059 (SNS rollover returns 0) [5](#0-4)

### Citations

**File:** rs/nns/governance/src/governance.rs (L6724-6725)
```rust
                    let reward = (used_voting_rights * total_available_e8s_equivalent_float
                        / total_voting_rights) as u64;
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

**File:** rs/sns/governance/src/governance.rs (L5974-5978)
```rust
                let neuron_reward_e8s =
                    rewards_purse_e8s * (neuron_reward_shares / total_reward_shares);

                // Round down, and convert to u64.
                let neuron_reward_e8s = u64::try_from(neuron_reward_e8s).unwrap_or_else(|err| {
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
