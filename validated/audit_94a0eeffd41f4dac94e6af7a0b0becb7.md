### Title
NNS Governance Async Vote Cascade Reads Live Neuron Followees After Proposal Deadline, Enabling Post-Deadline Auto-Vote Manipulation - (File: `rs/nns/governance/src/voting.rs`)

---

### Summary

The NNS governance's `process_voting_state_machines` timer processes pending cascade votes by reading **live** neuron followees from the neuron store rather than a snapshot taken at vote-cast time. When a large cascade is split across multiple messages due to instruction limits, a neuron controller can change their neuron's followees **after the proposal deadline has passed** but before the cascade processes their ballot, effectively casting or redirecting their auto-vote (following-based vote) after the voting period has ended.

---

### Finding Description

**Async cascade deferral mechanism.** When a neuron votes and triggers a following cascade, the NNS governance processes the cascade in `cast_vote_and_cascade_follow` (`rs/nns/governance/src/voting.rs:122-180`). If the cascade exceeds the soft instruction limit, it is persisted in `VOTING_STATE_MACHINES` stable storage and deferred to a timer job (`schedule_vote_processing`, `rs/nns/governance/canister/canister.rs:94-98`) that fires every 3 seconds, calling `process_voting_state_machines` (`rs/nns/governance/src/voting.rs:225-268`).

**Live follower index read.** Inside `continue_processing` (`rs/nns/governance/src/voting.rs:506-573`), two live reads occur:

1. `add_followers_to_check` (`rs/nns/governance/src/voting.rs:462-475`) calls `neuron_store.get_followers_by_followee_and_topic`, which reads the **current** follower index — not a snapshot from when the triggering vote was cast.

2. For each follower in `followers_to_check`, `neuron_store.neuron_would_follow_ballots` (`rs/nns/governance/src/neuron_store.rs:774-787`) is called, which loads the neuron's **current** `followees` field and passes it to `would_follow_ballots` (`rs/nns/governance/src/neuron/types.rs:462-504`).

**No deadline guard in cascade processing.** `process_voting_state_machines` contains no check that the proposal's voting deadline has passed before filling in ballots. It processes all machines in `VOTING_STATE_MACHINES` unconditionally.

**`follow()` updates the index immediately.** The `follow` function (`rs/nns/governance/src/governance.rs:5718-5791`) updates `neuron.followees` and calls `update_neuron_indexes` synchronously, so any change to followees is immediately reflected in the follower index that `add_followers_to_check` reads.

**Attack window.** Between the moment a triggering vote is cast (creating a pending cascade machine) and the moment `add_followers_to_check` runs for that neuron in the timer, an attacker can:
- Add their neuron as a follower of the voting neuron (to receive a Yes or No auto-vote)
- Switch their neuron's followees from a Yes-voter to a No-voter (or vice versa)
- Remove all followees to suppress their ballot

Because `cast_vote` (`rs/nns/governance/src/voting.rs:488-503`) only fills in `Unspecified` ballots, the attacker's ballot must still be `Unspecified` — but that is the normal state for a neuron that has not yet voted directly.

After the cascade completes, `process_voting_state_machines` calls `recompute_proposal_tally` and `process_proposal` (`rs/nns/governance/src/voting.rs:250-252`), so the post-deadline cascade votes are counted in the final tally and can decide the proposal.

---

### Impact Explanation

A neuron controller whose ballot is still `Unspecified` can observe a pending cascade (e.g., by querying proposal ballot state), wait for the proposal deadline to pass, then call `follow()` to set or change their followees. The cascade timer will then fill in their ballot based on the **new** followees, effectively casting a vote after the voting period has closed. This allows:

- Retroactively opting into a Yes or No vote after observing the final tally trend
- Switching from following a Yes-voter to following a No-voter (or vice versa) to tip a close proposal
- Suppressing an auto-vote by removing followees

The affected parties are honest voters whose intended outcome can be overridden by post-deadline ballot manipulation. The impact is governance integrity loss on the NNS, which controls the entire Internet Computer protocol.

---

### Likelihood Explanation

Large cascades that exceed the per-message instruction limit are realistic in the NNS, which has hundreds of thousands of neurons with complex following graphs. The timer fires every 3 seconds, giving an attacker a window of at least one timer interval between the triggering vote and `add_followers_to_check` running. If the cascade spans multiple timer rounds (common for deep following chains), the window extends to 6, 9, or more seconds. The attacker needs only to:

1.