Audit Report

## Title
SNS Governance `distribute_rewards` Returns Early on `u64` Overflow Without Advancing `latest_reward_event`, Causing Self-Perpetuating Reward Freeze - (`File: rs/sns/governance/src/governance.rs`)

## Summary

In `rs/sns/governance/src/governance.rs`, `distribute_rewards` computes `rewards_purse_e8s` as a `Decimal` and, if it overflows `u64`, executes an early `return` at line 5888 without updating `self.proto.latest_reward_event`. Because `new_rounds_count` is derived from the stale `end_timestamp_seconds` of that unchanged event, every subsequent call computes an even larger purse (more elapsed rounds), making the overflow self-perpetuating. All proposals in `ReadyToSettle` are never settled, neuron maturity is never increased, and the accumulated rewards purse is permanently discarded on every heartbeat.

## Finding Description

**Root cause — early return without state update:**

At lines 5878–5890, after computing `rewards_purse_e8s` as a `Decimal`, the function attempts a narrowing conversion:

```rust
let total_available_e8s_equivalent = Some(match u64::try_from(rewards_purse_e8s) {
    Ok(ok) => ok,
    Err(err) => {
        log!(ERROR, "Looks like the rewards purse ({}) overflowed u64: {}. \
             Therefore, we stop the current attempt to distribute voting rewards.",
             rewards_purse_e8s, err);
        return;   // ← no state update
    }
});
```

The only place `self.proto.latest_reward_event` is written is at lines 6084–6092, which is never reached on this path.

**Self-perpetuating loop:**

On every subsequent call:
1. `reward_start_timestamp_seconds` (line 5808–5811) reads `end_timestamp_seconds` from the stale, unchanged `latest_reward_event`.
2. `new_rounds_count` (line 5812–5814) = `(now − stale_end) / round_duration`, which grows monotonically as wall-clock time advances.
3. `rewards_purse_e8s` (line 5854–5875) = `e8s_equivalent_to_be_rolled_over()` + `supply × rate × new_rounds_count`. With `new_rounds_count` strictly increasing, the purse only grows larger, guaranteeing repeated overflow.
4. The early `return` fires again; `latest_reward_event` remains stale. The cycle repeats indefinitely.

**Rollover path does not help:**

`e8s_equivalent_to_be_rolled_over()` (types.rs lines 2054–2060) returns `total_available_e8s_equivalent` only when `rewards_rolled_over()` is true (i.e., `settled_proposals.is_empty()`). Even if this returns 0, the dominant term `supply × rate × new_rounds_count` alone is sufficient to overflow once enough rounds have accumulated.

**Downstream consequences:**

- `considered_proposals` (line 5822–5823) are never settled; their `reward_event_end_timestamp_seconds` is never set and ballots are never cleared (line 6080).
- Neuron maturity is never incremented (line 5994).
- `max_number_of_proposals_with_ballots` can be exhausted, blocking new proposals.

**No existing guard prevents this:** The only check before the overflow point is `new_rounds_count == 0` (line 5815), which does not fire once time has advanced past the stale event's end.

## Impact Explanation

Permanent loss of voting rewards (neuron maturity) for all SNS neuron holders who voted on proposals in `ReadyToSettle` state. The rewards purse — potentially representing a significant fraction of the SNS token supply — is silently discarded on every governance heartbeat. Proposals are permanently stuck in `ReadyToSettle`, consuming memory and eventually blocking new proposal submission. This constitutes a significant SNS security impact with concrete, irreversible user and protocol harm, matching the **High** bounty tier: "Significant SNS security impact with concrete user or protocol harm."

## Likelihood Explanation

Overflow requires `rewards_purse_e8s > u64::MAX ≈ 1.844 × 10¹⁹ e8s`. Concrete reachable scenarios:

- **Large supply + high rate + extended downtime:** An SNS with 10¹² tokens (10²⁰ e8s) at 10% annual reward rate needs ~67 missed daily rounds (~67 days of canister unavailability) to overflow. SNS governance canisters can be paused during upgrades or due to bugs; 67 days is within the range of extended incidents.
- **Reduced `round_duration_seconds` via governance proposal:** A passing `ManageNervousSystemParameters` proposal that reduces `round_duration_seconds` causes `new_rounds_count` to spike on the very next `distribute_rewards` call (all elapsed time divided by the new smaller duration). With a large supply, a single such proposal can immediately trigger overflow. This path requires only a passing SNS governance vote — no privileged system access.
- **Accumulated rollover:** Repeated rollover events (no proposals to settle) accumulate `total_available_e8s_equivalent` in `latest_reward_event`, which is then added to the next purse computation, lowering the threshold for overflow.

Once triggered, the condition is permanent without an explicit governance intervention to zero out the reward rate — which itself requires proposals to pass while the system is degraded.

## Recommendation

1. **Saturating conversion (preferred):** Replace the early return with saturation so reward distribution always proceeds:
   ```rust
   let total_available_e8s_equivalent = Some(
       u64::try_from(rewards_purse_e8s).unwrap_or(u64::MAX)
   );
   ```
2. **Advance `end_timestamp_seconds` on overflow:** If the early return is kept for any reason, update `latest_reward_event.end_timestamp_seconds` to `reward_event_end_timestamp_seconds` before returning, so time advances and `new_rounds_count` does not grow unboundedly.
3. **Validate `round_duration_seconds` changes:** In `perform_manage_nervous_system_parameters`, check that reducing `round_duration_seconds` would not cause `new_rounds_count × supply × rate` to exceed `u64::MAX` given the current state.

## Proof of Concept

**Deterministic unit test plan** (safe, local, no mainnet interaction):

```rust
#[test]
fn test_distribute_rewards_overflow_is_self_perpetuating() {
    // 1. Construct SNS governance with:
    //    - token supply = 10^20 e8s (10^12 tokens)
    //    - initial_reward_rate_basis_points = 1000 (10%/year)
    //    - round_duration_seconds = 86400 (1 day)
    //    - latest_reward_event.end_timestamp_seconds = T0
    //    - one proposal in ReadyToSettle state

    // 2. Set env.now() = T0 + 86400 * 100  (100 missed rounds)
    //    Expected purse ≈ 10^20 * 0.10/365 * 100 ≈ 2.74e18 — does NOT overflow yet.

    // 3. Set env.now() = T0 + 86400 * 700  (700 missed rounds)
    //    Expected purse ≈ 10^20 * 0.10/365 * 700 ≈ 1.92e19 > u64::MAX — OVERFLOWS.
    //    Call distribute_rewards(supply).
    //    Assert: latest_reward_event.end_timestamp_seconds == T0  (unchanged).
    //    Assert: proposal still in ReadyToSettle.
    //    Assert: neuron maturity unchanged.

    // 4. Advance time by one more round (T0 + 86400 * 701).
    //    Call distribute_rewards(supply) again.
    //    Assert: latest_reward_event.end_timestamp_seconds still == T0.
    //    Assert: new_rounds_count is now 701, purse is larger — overflow persists.

    // 5. Confirm the system is permanently stuck by repeating step 4 N times.
}
```

This test exercises the exact code path at lines 5808–5814 and 5878–5890 of `rs/sns/governance/src/governance.rs` and can be added directly to `rs/sns/governance/src/governance/assorted_governance_tests.rs` alongside the existing `no_new_reward_event_when_there_are_no_new_proposals` test.