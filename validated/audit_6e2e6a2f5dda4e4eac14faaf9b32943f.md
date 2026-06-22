### Title
Wait-for-Quiet Deadline Extension Bypassed by Majority-Reaching Late Vote — (`rs/nns/governance/src/governance.rs`, `rs/sns/governance/src/proposal.rs`)

### Summary
Both NNS and SNS governance implement a "wait-for-quiet" (WFQ) mechanism to deter last-minute voting. However, WFQ only extends the deadline when the vote **flips** (majority switches sides). It does not extend the deadline when a late voter reinforces the current winning side and pushes the tally over the majority threshold. Because the live tally (`latest_tally`, `total_potential_voting_power`) is publicly readable by any anonymous caller via `get_proposal_info`, an attacker with sufficient voting power can observe the exact state of a proposal near its deadline and cast a decisive vote that immediately closes the proposal — without triggering any deadline extension and without giving opponents time to respond.

### Finding Description

**Publicly readable tally**

`get_proposal_info` is a `#[query]` endpoint with no authentication requirement. It returns a `ProposalInfo` that includes `latest_tally` (yes, no, total) and `total_potential_voting_power`: [1](#0-0) [2](#0-1) 

Any anonymous principal can call this query at any time during the voting period and learn the exact current vote distribution and total eligible voting power.

**WFQ only triggers on a flip, not on a majority-reaching reinforcement**

`evaluate_wait_for_quiet` (NNS) returns early — without extending the deadline — in two relevant cases:

1. When the new tally already reaches a deciding majority (`new_tally.yes >= deciding_amount_yes` or `new_tally.no >= deciding_amount_no`).
2. When the vote has not flipped sides (`!vote_has_turned`). [3](#0-2) 

The identical logic exists in SNS governance: [4](#0-3) 

**The gap**: if a proposal is currently winning (yes > no, but yes < majority), and an attacker votes Yes near the deadline with just enough power to push yes ≥ majority, the first guard (`new_tally.yes >= deciding_amount_yes`) fires and WFQ returns immediately without any extension. The proposal is decided on the spot.

### Impact Explanation

An attacker who holds sufficient voting power can:

1. Monitor the live tally via unauthenticated `get_proposal_info` queries throughout the voting period.
2. Wait until the deadline is imminent and no further opposition votes are expected.
3. Cast a single vote that pushes the tally over the majority threshold.
4. Because WFQ only extends on a flip, the proposal is decided immediately with no additional time granted to opponents.

This allows a well-resourced neuron holder to strategically time their vote to maximize information advantage — observing whether opposition materializes before committing — and then lock in the outcome at the last moment. Opponents who might have voted in response have no recourse once the proposal is decided.

The impact is governance outcome manipulation: proposals that would have been rejected (or at least contested) if all parties voted simultaneously can be passed by a patient attacker who withholds their decisive vote until the final moments.

### Likelihood Explanation

- The tally is fully public and readable by any anonymous caller with no rate limiting at the governance layer.
- The attacker needs only a neuron with enough voting power to push the tally over majority — a realistic condition for large ICP holders or coordinated groups.
- No special access, admin keys, or subnet-majority corruption is required.
- The attack is entirely passive until the final vote, making it difficult to detect or prevent.
- Likelihood is **medium**: it requires meaningful voting power, but the information advantage is available to every participant at zero cost.

### Recommendation

- **Short term**: Extend the WFQ deadline not only on a flip but also when a late vote (cast within the final `wait_for_quiet_deadline_increase_seconds` window) causes the tally to cross the majority threshold for the first time. This gives opponents a window to respond even when the decisive vote reinforces the winning side.
- **Long term**: Consider whether `total_potential_voting_power` and per-neuron ballot voting power should be withheld or blinded until after the voting period closes, reducing the precision of strategic calculations. Document the known residual late-voting incentive in the governance specification.

### Proof of Concept

**Setup (NNS)**:
- Total voting power: 100. Majority threshold: 51.
- Neuron A (attacker): 15 VP, has not voted.
- Current tally at T − 60 s: Yes = 40, No = 30, Undecided = 30.

**Step 1** — Attacker calls `get_proposal_info` (anonymous query) and reads:
```
latest_tally: { yes: 40, no: 30, total: 100 }
total_potential_voting_power: 100
```
Attacker computes: needs 11 more Yes votes to reach majority (51). They hold 15 VP. The proposal is already winning; no flip is needed.

**Step 2** — At T − 1 s, attacker calls `manage_neuron { RegisterVote { vote: Yes } }`.

**Step 3** — `recompute_tally` produces new tally: Yes = 55, No = 30.

**Step 4** — `evaluate_wait_for_quiet` is called:
```rust
let deciding_amount_yes = 100 / 2 + 1; // = 51
if new_tally.yes >= deciding_amount_yes { // 55 >= 51 → true
    return; // WFQ exits immediately, no extension
}
``` [5](#0-4) 

**Step 5** — `process_proposal` decides the proposal as Adopted. No-voters had no time to respond. The identical code path exists in SNS governance at: [6](#0-5)

### Citations

**File:** rs/nns/governance/canister/canister.rs (L357-361)
```rust
#[query]
fn get_proposal_info(id: ProposalId) -> Option<ProposalInfo> {
    debug_log("get_proposal_info");
    with_governance(|governance| governance.get_proposal_info(&caller(), id))
}
```

**File:** rs/nns/governance/src/pb/proposal_conversions.rs (L558-566)
```rust
    let latest_tally = data.latest_tally.map(|x| x.into());
    let decided_timestamp_seconds = data.decided_timestamp_seconds;
    let executed_timestamp_seconds = data.executed_timestamp_seconds;
    let failed_timestamp_seconds = data.failed_timestamp_seconds;
    let failure_reason = data.failure_reason.clone().map(|x| x.into());
    let reward_event_round = data.reward_event_round;
    let derived_proposal_information = data.derived_proposal_information.clone().map(|x| x.into());
    let total_potential_voting_power = data.total_potential_voting_power;
    let success_value = data
```

**File:** rs/nns/governance/src/governance.rs (L646-664)
```rust
        let current_deadline = wait_for_quiet_state.current_deadline_timestamp_seconds;
        let deciding_amount_yes = new_tally.total / 2 + 1;
        let deciding_amount_no = new_tally.total.div_ceil(2);
        if new_tally.yes >= deciding_amount_yes
            || new_tally.no >= deciding_amount_no
            || now_seconds > current_deadline
        {
            return;
        }

        // Returns whether the vote has turned, i.e. if the vote is now yes, when it was
        // previously no, or if the vote is now no if it was previously yes.
        fn vote_has_turned(old_tally: &Tally, new_tally: &Tally) -> bool {
            (old_tally.yes > old_tally.no && new_tally.yes <= new_tally.no)
                || (old_tally.yes <= old_tally.no && new_tally.yes > new_tally.no)
        }
        if !vote_has_turned(old_tally, new_tally) {
            return;
        }
```

**File:** rs/sns/governance/src/proposal.rs (L2131-2149)
```rust
        let current_deadline = wait_for_quiet_state.current_deadline_timestamp_seconds;
        let deciding_amount_yes = new_tally.total / 2 + 1;
        let deciding_amount_no = new_tally.total.div_ceil(2);
        if new_tally.yes >= deciding_amount_yes
            || new_tally.no >= deciding_amount_no
            || now_seconds > current_deadline
        {
            return;
        }

        // Returns whether the tally result has turned, i.e. if the result now
        // favors yes, but it used to favor no or vice versa.
        fn vote_has_turned(old_tally: &Tally, new_tally: &Tally) -> bool {
            (old_tally.yes > old_tally.no && new_tally.yes <= new_tally.no)
                || (old_tally.yes <= old_tally.no && new_tally.yes > new_tally.no)
        }
        if !vote_has_turned(old_tally, new_tally) {
            return;
        }
```
