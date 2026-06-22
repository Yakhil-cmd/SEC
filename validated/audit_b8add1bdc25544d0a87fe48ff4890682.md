### Title
SNS Governance Silently Discards Earned Voting Rewards for Neurons Not Found During Distribution - (`rs/sns/governance/src/governance.rs`)

### Summary

In SNS governance's `distribute_rewards` function, when a neuron that voted on settled proposals cannot be found during the reward distribution pass, its earned voting rewards are permanently and silently discarded. No debt is recorded, no escrow is held, and the neuron owner has no mechanism to claim the owed maturity later. This is a direct analog to M-07: a service that is supposed to deliver owed value to a user silently drops it when a resource/state check fails, with no tracking and no recourse.

### Finding Description

`distribute_rewards` in `rs/sns/governance/src/governance.rs` iterates over all neurons that cast eligible votes on settled proposals and attempts to credit each neuron's maturity. At the distribution loop:

```rust
for (neuron_id, neuron_reward_shares) in neuron_id_to_reward_shares {
    let neuron: &mut Neuron = match self.get_neuron_result_mut(&neuron_id) {
        Ok(neuron) => neuron,
        Err(err) => {
            log!(
                ERROR,
                "Cannot find neuron {}, despite having voted with power {} \
                 in the considered reward period. The reward that should have been \
                 distributed to this neuron is simply skipped ...",
                ...
            );
            continue;   // ← reward permanently lost
        }
    };
``` [1](#0-0) 

After this loop, all considered proposals are immediately settled and their ballots cleared:

```rust
for pid in &considered_proposals {
    self.process_proposal(pid.id);
    ...
    p.reward_event_end_timestamp_seconds = Some(reward_event_end_timestamp_seconds);
    p.reward_event_round = new_reward_event_round;
    p.ballots.clear();   // ← no replay possible
}
``` [2](#0-1) 

Once ballots are cleared and the `RewardEvent` is committed, there is no on-chain record of the skipped amount, no pending claim entry, and no way to reconstruct the owed maturity. The governance canister retains the unspent portion of the reward purse (since `distributed_e8s_equivalent < total_available_e8s_equivalent`), but the individual neuron owner has no recourse.

The identical silent-skip pattern exists in NNS governance's `calculate_voting_rewards`:

```rust
} else {
    println!(
        "{}Cannot find neuron {}, despite having voted with power {} \
            in the considered reward period. The reward that should have been \
            distributed to this neuron is simply skipped ...",
        ...
    );
}
``` [3](#0-2) 

### Impact Explanation

A neuron owner who voted on one or more proposals and whose neuron is subsequently absent from the store at reward-distribution time permanently loses the maturity they earned. The loss is:

- **Untracked**: no mapping of `neuron_id → owed_maturity` is written anywhere.
- **Irrecoverable**: proposals are settled and ballots cleared in the same call, so the calculation cannot be replayed.
- **Invisible to the user**: the only signal is an ERROR log entry; no on-chain event or error is surfaced to the neuron owner.

The governance canister itself is not harmed (the unspent purse rolls over or is simply under-distributed), but the individual user suffers a permanent maturity loss with no governance-provided remedy.

### Likelihood Explanation

In SNS governance, neurons can be dissolved and their stake fully disbursed. Whether a dissolved neuron is subsequently pruned from `self.proto.neurons` depends on SNS-specific cleanup logic. The code's own comment — *"The reward that should have been distributed to this neuron is simply skipped"* — confirms the developers anticipated this case as reachable. Additionally, any future canister upgrade that prunes zero-stake neurons, or any bug that corrupts the neuron map, would trigger this path for all affected voters in the same reward round. The entry path requires only normal user actions (vote, dissolve, disburse) and no privileged access.

### Recommendation

1. Before clearing ballots, record any skipped `(neuron_id, owed_maturity_e8s)` pairs in a persistent `unclaimed_rewards` map in governance state.
2. Expose a `claim_unclaimed_reward(neuron_id)` update endpoint that credits the maturity to a neuron controlled by the caller if an unclaimed entry exists.
3. Alternatively, hold the owed maturity in a dedicated escrow subaccount keyed by neuron ID, so the owner can claim it even after the reward event is settled.

### Proof of Concept

1. Alice creates an SNS neuron and votes on proposals P1 and P2 during reward round R.
2. Before round R's reward distribution runs, Alice dissolves her neuron and disburses her full stake. The neuron is removed from (or becomes unfindable in) `self.proto.neurons`.
3. `distribute_rewards` runs for round R. Alice's `neuron_id` appears in `neuron_id_to_reward_shares` (her ballots were recorded), but `get_neuron_result_mut` returns `Err`.
4. The code logs an ERROR and executes `continue` — Alice's maturity reward is silently dropped.
5. P1 and P2 are marked `Settled` and their ballots cleared. The reward event is committed.
6. Alice has no on-chain mechanism to claim her owed maturity. Her only recourse is to contact SNS governance administrators and request a manual governance proposal to compensate her — exactly the centralized, friction-heavy path described in M-07.

### Citations

**File:** rs/sns/governance/src/governance.rs (L5954-5970)
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
```

**File:** rs/sns/governance/src/governance.rs (L6013-6081)
```rust
        for pid in &considered_proposals {
            // Before considering a proposal for reward, it must be fully processed --
            // because we're about to clear the ballots, so no further processing will be
            // possible.
            self.process_proposal(pid.id);

            let p = match self.get_proposal_data_mut(*pid) {
                Some(p) => p,
                None => {
                    log!(
                        ERROR,
                        "Cannot find proposal {}, despite it being considered for rewards distribution.",
                        pid.id
                    );
                    debug_assert!(
                        false,
                        "It appears that proposal {} has been deleted out from under us \
                         while we were distributing rewards. This should never happen. \
                         In production, this would be quietly swept under the rug and \
                         we would continue processing. Current state (Governance):\n{:#?}",
                        pid.id, self.proto,
                    );
                    continue;
                }
            };

            if p.status() == ProposalDecisionStatus::Open {
                log!(
                    ERROR,
                    "Proposal {} was considered for reward distribution despite \
                     being open. We will now force the proposal's status to be Rejected.",
                    pid.id
                );
                debug_assert!(
                    false,
                    "This should be unreachable. Current governance state:\n{:#?}",
                    self.proto,
                );

                // The next two statements put p into the Rejected status. Thus,
                // process_proposal will consider that it has nothing more to do
                // with the p.
                p.decided_timestamp_seconds = now;
                p.latest_tally = Some(Tally {
                    timestamp_seconds: now,
                    yes: 0,
                    no: 0,
                    total: 0,
                });
                debug_assert_eq!(
                    p.status(),
                    ProposalDecisionStatus::Rejected,
                    "Failed to force ProposalData status to become Rejected. p:\n{p:#?}",
                );
            }

            // This is where the proposal becomes Settled, at least in the eyes
            // of the ProposalData::reward_status method.
            p.reward_event_end_timestamp_seconds = Some(reward_event_end_timestamp_seconds);
            p.reward_event_round = new_reward_event_round;

            // Ballots are used to determine two things:
            //   1. (obviously and primarily) whether to execute the proposal.
            //   2. rewards
            // At this point, we no longer need ballots for either of these
            // things, and since they take up a fair amount of space, we take
            // this opportunity to jettison them.
            p.ballots.clear();
        }
```

**File:** rs/nns/governance/src/governance.rs (L6733-6742)
```rust
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
