The code confirms the claim. Let me verify the critical detail about what `total_proposal_amounts_tokens` counts.

Audit Report

## Title
Check-Effects-Interactions Violation in `treasury_valuation_if_proposal_amount_is_small_enough_or_err` Allows Bypassing 7-Day Submission-Time Treasury Transfer Limit — (`rs/sns/governance/src/proposal.rs`)

## Summary
In `treasury_valuation_if_proposal_amount_is_small_enough_or_err`, the 7-day rolling `spent_tokens` value is read from state before an async inter-canister call (`assess_treasury_balance`), and the limit check is performed using that stale value after the await returns. Because the IC execution model allows other messages to be processed during an `await`, two concurrent `make_proposal` calls can both read the same `spent_tokens = 0`, both pass the submission-time limit check, and both be inserted into governance state with a combined amount exceeding the 7-day cap. The execution-time check in `perform_transfer_sns_treasury_funds` prevents actual treasury drain, but the submission-time invariant is broken, enabling governance disruption and griefing.

## Finding Description
`treasury_valuation_if_proposal_amount_is_small_enough_or_err` (`rs/sns/governance/src/proposal.rs`, lines 770–817) implements the submission-time 7-day rolling limit check:

1. **Line 780:** `spent_tokens` is computed synchronously via `action.recent_amount_total_tokens(proposals, env.now())`, which calls `total_proposal_amounts_tokens`. That function filters proposals by `executed_timestamp_seconds < min_executed_timestamp_seconds` (line 2743), meaning it counts only **successfully executed** proposals. Open/pending proposals have `executed_timestamp_seconds = 0` and are always skipped.

2. **Lines 784–790:** `assess_treasury_balance(...).await?` makes async inter-canister calls to the CMC and swap canister. This is a yield point; the IC scheduler can process other ingress messages here.

3. **Lines 801–813:** The limit check `proposal_amount_tokens > allowance_remainder_tokens` uses the `spent_tokens` captured in step 1, which is now stale.

`make_proposal` (`rs/sns/governance/src/governance.rs`, line 3467) calls `validate_and_render_proposal` (which invokes the above function) **before** inserting the proposal into state. Two concurrent `make_proposal` messages for `TransferSnsTreasuryFunds` can therefore both enter the function, both read `spent_tokens = 0`, both yield at the `assess_treasury_balance` await, and both pass the limit check independently. Both proposals are then inserted into governance state.

The execution-time guard in `perform_transfer_sns_treasury_funds` (`rs/sns/governance/src/governance.rs`, lines 3000–3005) calls `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err`, which re-reads `spent_tokens` synchronously from current state. The `IN_PROGRESS_PROPOSAL_ID` lock (lines 2987–2998) serializes execution of `TransferSnsTreasuryFunds` proposals, ensuring proposal B's execution-time check sees proposal A's already-executed amount. This prevents actual treasury drain but does not prevent the submission-time bypass.

## Impact Explanation
The concrete impact is governance disruption and griefing against an SNS. Two proposals, each individually within the 7-day limit, can be submitted and adopted by governance with a combined amount exceeding the cap. The second proposal will fail at execution time (after voters have already approved it), wasting governance bandwidth, misleading voters, and consuming the proposer's `reject_cost_e8s` stake. The 7-day submission-time cap is a stated security invariant for SNS treasury protection; bypassing it at submission time undermines the governance safety model even though the execution-time check provides a backstop. This constitutes a significant SNS governance security impact with concrete protocol harm, qualifying as High under the "Significant SNS security impact with concrete user or protocol harm" category.

## Likelihood Explanation
Any SNS token holder controlling two neurons that each meet the `reject_cost_e8s` and `neuron_minimum_dissolve_delay_to_vote_seconds` requirements can trigger this. The attack requires only submitting two `TransferSnsTreasuryFunds` proposals in rapid succession via standard ingress messages, which is straightforward and requires no privileged access. The attack is repeatable as long as the attacker retains sufficient neuron stake.

## Recommendation
Apply the effects-before-interaction pattern: record the proposal's intended amount as a "pending" entry in governance state **before** making the async `assess_treasury_balance` call, and include pending proposals in the `spent_tokens` calculation. Remove the pending entry if validation fails or the proposal is rejected.

Alternatively, move the `spent_tokens` read to **after** the `assess_treasury_balance` await returns, so the limit check always uses up-to-date state. This is simpler but requires care to ensure the iterator over proposals is re-acquired post-await.

The execution-time check in `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err` should be retained as defense-in-depth regardless of which fix is applied.

## Proof of Concept
1. Deploy SNS governance with a treasury of 1,000,000 SNS tokens; 7-day limit is 250,000 tokens (25%).
2. Attacker controls two neurons, each with stake ≥ `reject_cost_e8s` and dissolve delay ≥ `neuron_minimum_dissolve_delay_to_vote_seconds`.
3. Attacker sends two `make_proposal` ingress messages for `TransferSnsTreasuryFunds` with `amount_e8s = 200_000 * E8` in rapid succession.
4. Governance processes message A: reads `spent_tokens = 0` (no executed proposals), then yields at `assess_treasury_balance`.
5. While awaiting, IC processes message B: reads `spent_tokens = 0` (same state, proposal A not yet inserted), then yields at `assess_treasury_balance`.
6. Message A's await completes: checks `200,000 > 250,000 - 0`? No → passes. Proposal A inserted.
7. Message B's await completes: checks `200,000 > 250,000 - 0`? No → passes. Proposal B inserted.
8. Both proposals are open for voting. Governance adopts both.
9. Proposal A executes: execution-time check sees `spent = 0`, `200,000 ≤ 250,000` → transfer succeeds; `executed_timestamp_seconds` is set.
10. Proposal B executes: execution-time check sees `spent = 200,000`, `200,000 > 250,000 - 200,000 = 50,000` → execution fails with `PreconditionFailed`.
11. Result: Proposal B was adopted by governance but fails silently at execution. The 7-day submission-time limit was bypassed; governance resources were wasted; voters were misled.

A deterministic integration test using PocketIC can reproduce this by submitting two concurrent `make_proposal` calls and asserting that both proposals are inserted into state with a combined amount exceeding the 7-day cap, then verifying that proposal B's execution returns `PreconditionFailed`.