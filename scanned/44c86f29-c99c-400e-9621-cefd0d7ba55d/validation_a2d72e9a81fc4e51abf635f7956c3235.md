### Title
Undistributed Voting Rewards from Absent Neurons Are Permanently Lost Instead of Being Rolled Over — (`rs/nns/governance/src/governance.rs`)

### Summary

In the NNS governance canister, when `calculate_voting_rewards` distributes voting rewards, any neuron that cast a ballot but is no longer present in the neuron store at reward-distribution time has its share of the reward pool **silently dropped**. The gap between `total_available_e8s_equivalent` and `actually_distributed_e8s_equivalent` is never rolled over to the next reward event, causing a permanent, irrecoverable reduction in the total maturity distributed to the ecosystem. This is the direct IC analog of the CoreDAO `distributePowerReward` bug where undelegated coin rewards were not considered, leading to incomplete reward accounting.

---

### Finding Description

`calculate_voting_rewards` in `rs/nns/governance/src/governance.rs` iterates over every `(neuron_id, used_voting_rights)` pair collected from settled proposal ballots. For each neuron it checks whether the neuron still exists in the store:

```rust
for (neuron_id, used_voting_rights) in voters_to_used_voting_right {
    if self.neuron_store.contains(neuron_id) {
        let reward = (used_voting_rights * total_available_e8s_equivalent_float
            / total_voting_rights) as u64;
        reward_distribution.add_reward(neuron_id, reward);
        actually_distributed_e8s_equivalent += reward;
    } else {
        // reward is silently dropped
    }
}
``` [1](#0-0) 

When a neuron is absent, its proportional share of `total_available_e8s_equivalent` is neither credited to any neuron nor rolled over. The `RewardEvent` records `distributed_e8s_equivalent < total_available_e8s_equivalent`, but the rollover logic in `e8s_equivalent_to_be_rolled_over` only returns a non-zero value when `settled_proposals` is **empty**:

```rust
pub(crate) fn e8s_equivalent_to_be_rolled_over(&self) -> u64 {
    if self.rewards_rolled_over() {
        self.total_available_e8s_equivalent
    } else {
        0   // ← returned whenever ANY proposals were settled
    }
}

pub(crate) fn rewards_rolled_over(&self) -> bool {
    self.settled_proposals.is_empty()
}
``` [2](#0-1) 

Because a normal reward round always has settled proposals, `e8s_equivalent_to_be_rolled_over` always returns `0`, and the undistributed portion is permanently lost. The next round's `total_available_e8s_equivalent` is computed purely from the current ICP supply fraction plus any prior rollover — it never includes the gap from the current round. [3](#0-2) 

The same structural flaw exists in the SNS governance `distribute_rewards` function: [4](#0-3) 

---

### Impact Explanation

**Vulnerability class:** Governance reward accounting bug / ledger conservation bug.

Every time a neuron that participated in voting is absent from the neuron store at reward-distribution time, its proportional share of the ICP maturity reward pool is permanently destroyed. Over time this silently deflates the total maturity distributed to all NNS participants. The `RewardEvent` itself records the discrepancy (`distributed_e8s_equivalent` < `total_available_e8s_equivalent`), confirming the loss is real and observable on-chain. [5](#0-4) 

**Impact: 3** — Maturity rewards are permanently lost from the ecosystem; the magnitude scales with the voting power of absent neurons.

---

### Likelihood Explanation

A neuron can be absent at reward time through neuron merging: a neuron that voted (directly or via following) can be merged into another neuron by its controller before the daily reward distribution fires. After a merge the source neuron's ID is gone from the store; its ballot remains in the settled proposal, but its reward is dropped. The `merge_neurons` path does not check whether the source neuron has pending reward ballots in `ReadyToSettle` proposals — it only blocks merges involving **open** proposals. [6](#0-5) 

The reward distribution window is up to 24 hours (one reward round). Any neuron owner who votes and then merges their source neuron into another neuron within that window triggers the loss. This is reachable by any unprivileged `manage_neuron` caller with no special permissions.

**Likelihood: 2** — Requires deliberate or accidental merge within the reward window; not a common operation, but fully reachable without privilege.

---

### Recommendation

When a neuron is absent during reward distribution, its proportional reward share should be accumulated into a "dust" counter and either:

1. **Redistributed proportionally** among the neurons that were successfully rewarded in the same round, or
2. **Rolled over** into the next reward event's available pool, analogous to how the entire pool is rolled over when `settled_proposals` is empty.

The simplest fix is to track `undistributed_e8s` and add it back into the next round's `rolling_over_from_previous_reward_event_e8s_equivalent`, regardless of whether `settled_proposals` is empty.

---

### Proof of Concept

1. Neuron A (large voting power) votes YES on Proposal P via following. Proposal P reaches its voting deadline and enters `ReadyToSettle`.
2. Neuron A's controller calls `manage_neuron { Merge { source_neuron_id: A } }` targeting Neuron B. The merge succeeds because Proposal P is no longer `Open` — only open proposals block merges.
3. The daily `distribute_voting_rewards_to_neurons` fires. `calculate_voting_rewards` finds Neuron A's ballot in Proposal P's settled ballots, but `self.neuron_store.contains(neuron_a_id)` returns `false`.
4. Neuron A's reward share is silently skipped; `actually_distributed_e8s_equivalent` is less than `total_available_e8s_equivalent` by exactly Neuron A's proportional share.
5. `e8s_equivalent_to_be_rolled_over()` returns `0` because `settled_proposals` is non-empty.
6. The next round's reward pool does not include the lost amount. The maturity is permanently gone. [7](#0-6) [8](#0-7)

### Citations

**File:** rs/nns/governance/src/governance.rs (L6651-6654)
```rust
        let rolling_over_from_previous_reward_event_e8s_equivalent =
            latest_reward_event.e8s_equivalent_to_be_rolled_over();
        let total_available_e8s_equivalent_float = (supply.get_e8s() as f64) * fraction
            + rolling_over_from_previous_reward_event_e8s_equivalent as f64;
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

**File:** rs/nns/governance/src/reward/calculation.rs (L120-147)
```rust
    pub(crate) fn e8s_equivalent_to_be_rolled_over(&self) -> u64 {
        if self.rewards_rolled_over() {
            self.total_available_e8s_equivalent
        } else {
            0
        }
    }

    /// Calculates the rounds_since_last_distribution in this event that should
    /// be "rolled over" into the next `RewardEvent`.
    ///
    /// Behavior:
    /// - If rewards were distributed for this event, then no rounds should be
    ///   rolled over, so this function returns 0.
    /// - Otherwise, this function returns
    ///   `rounds_since_last_distribution`.
    pub(crate) fn rounds_since_last_distribution_to_be_rolled_over(&self) -> u64 {
        if self.rewards_rolled_over() {
            self.rounds_since_last_distribution.unwrap_or(0)
        } else {
            0
        }
    }

    /// Whether this is a "rollover event", where no rewards were distributed.
    pub(crate) fn rewards_rolled_over(&self) -> bool {
        self.settled_proposals.is_empty()
    }
```

**File:** rs/sns/governance/src/governance.rs (L5954-5969)
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
```

**File:** rs/nns/governance/src/governance/merge_neurons.rs (L605-647)
```rust
fn is_neuron_involved_with_open_proposal(
    neuron_id: &NeuronId,
    subaccount: &Subaccount,
    proposal_data: &ProposalData,
) -> bool {
    // Only consider proposals that have not been decided yet.
    if proposal_data.status() != ProposalStatus::Open {
        return false;
    }

    // For most proposals, the neuron is "involved" exactly when it is the proposer.
    if proposal_data.proposer.as_ref() == Some(neuron_id) {
        return true;
    }

    // The one exception is ManageNeuron proposals. In this case, then a neuron
    // can be involved if the neuron is the one that the proposal operates on.
    if !proposal_data.is_manage_neuron() {
        return false;
    }

    match proposal_data
        .proposal
        .as_ref()
        .and_then(|proposal| proposal.managed_neuron())
    {
        Some(NeuronIdOrSubaccount::NeuronId(managed_neuron_id)) => managed_neuron_id == *neuron_id,
        Some(NeuronIdOrSubaccount::Subaccount(managed_subaccount)) => {
            managed_subaccount == subaccount.to_vec()
        }
        None => false,
    }
}

fn is_neuron_involved_with_open_proposals(
    neuron_id: &NeuronId,
    subaccount: &Subaccount,
    proposals: &BTreeMap<u64, ProposalData>,
) -> bool {
    proposals.values().any(|proposal_data| {
        is_neuron_involved_with_open_proposal(neuron_id, subaccount, proposal_data)
    })
}
```
