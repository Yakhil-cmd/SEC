### Title
Arithmetic Overflow in `get_median` Corrupts Canister Query Stats Accounting — (File: `rs/query_stats/src/state_machine.rs`)

### Summary
The `get_median` function in the query stats aggregation state machine performs an unchecked addition of two `u64` values when computing the average for even-length stat lists. A malicious subnet node (protocol peer below the consensus fault threshold) can submit a `QueryStatsPayload` with `u64::MAX` for any stat field, causing the addition `left + right` to overflow and produce a silently wrapped, incorrect median value that is then committed to replicated canister state.

### Finding Description
In `rs/query_stats/src/state_machine.rs`, the `get_median` function computes the median of a sorted list of `u64` values extracted from `QueryStats` records submitted by block proposers: [1](#0-0) 

When the list length is even, the function averages the two middle elements: [2](#0-1) 

The expression `(left + right) / 2_u8.into()` uses the standard `Add` trait on `u64`. In Rust release builds (as used in production IC replicas), integer overflow wraps silently. If `left` and `right` are both large (e.g., one node submits `u64::MAX` for `num_instructions`), the sum wraps to a small value, and the computed median is completely wrong.

This median is then applied to canister state via `apply_query_stats_to_canister`: [3](#0-2) 

The aggregated (overflowed) stat is multiplied by `num_nodes` and accumulated into `canister_query_stats`, permanently corrupting the canister's recorded resource usage in replicated state.

The aggregation triggers when more than 2/3 of nodes have submitted a record for an epoch: [4](#0-3) 

The `QueryStatsPayload` is submitted by block proposers (subnet nodes). There is no validation capping the values of `num_calls`, `num_instructions`, `ingress_payload_size`, or `egress_payload_size` before they enter the median computation.

### Impact Explanation
A malicious subnet node (protocol peer below the consensus fault threshold) submits a `QueryStatsPayload` with `num_instructions = u64::MAX` (or any value near `u64::MAX`). When an even number of nodes have submitted stats for an epoch, the median computation overflows, producing a wrapped (near-zero or arbitrary) value. This incorrect value is then multiplied by `num_nodes` and written into the canister's `total_query_stats` in replicated state. The corruption is deterministic across all honest replicas (since release-mode wrapping is deterministic), so it does not cause consensus divergence, but it permanently corrupts the canister's query stats accounting stored in certified state.

### Likelihood Explanation
Any single malicious subnet node acting as a block proposer can inject an inflated `QueryStatsPayload`. The overflow triggers whenever the number of nodes that have submitted stats for an epoch is even, which is a common case (e.g., 4-node subnets). No special privilege beyond being a subnet node is required.

### Recommendation
Replace the unchecked addition with a saturating or checked operation:

```rust
// Instead of:
(left + right) / 2_u8.into()

// Use:
left.saturating_add(right) / 2_u8.into()
// or, for exact midpoint without overflow:
left / 2 + right / 2 + (left % 2 + right % 2) / 2
```

Additionally, validate and cap incoming `QueryStats` field values at a reasonable maximum before inserting them into the aggregation state.

### Proof of Concept
1. A malicious subnet node submits a `QueryStatsPayload` with `num_instructions = u64::MAX` for canister X at epoch E.
2. One honest node submits `num_instructions = 1` for the same canister X at epoch E.
3. Both nodes advance to epoch E+1, triggering aggregation (2 nodes with stats = even count).
4. `get_median` sorts `[1, u64::MAX]`, `mid = 1`, `left = 1`, `right = u64::MAX`.
5. `(1u64 + u64::MAX)` wraps to `0` in release mode; median = `0 / 2 = 0`.
6. `apply_query_stats_to_canister` writes `0 * num_nodes = 0` to the canister's `num_instructions` total — the actual instructions are erased.
7. Alternatively, with `left = u64::MAX - 1` and `right = u64::MAX`, the sum wraps to `u64::MAX - 2`, yielding a wildly incorrect median that inflates the canister's stats. [1](#0-0) [5](#0-4)

### Citations

**File:** rs/query_stats/src/state_machine.rs (L84-105)
```rust
fn get_median<T: Default + Ord + Copy + Add<Output = T> + Div<Output = T> + From<u8>, F>(
    stats: &[&QueryStats],
    f: F,
) -> T
where
    F: FnMut(&&QueryStats) -> T,
{
    let mut values: Vec<T> = stats.iter().map(f).collect();
    values.sort_unstable();
    let mid = values.len() / 2;

    if values.len().is_multiple_of(2) {
        let left = values
            .get(mid.saturating_sub(1))
            .cloned()
            .unwrap_or(T::default());
        let right = values.get(mid).cloned().unwrap_or(T::default());
        (left + right) / 2_u8.into()
    } else {
        values.get(mid).cloned().unwrap_or(T::default())
    }
}
```

**File:** rs/query_stats/src/state_machine.rs (L128-154)
```rust
fn apply_query_stats_to_canister(
    aggregated_stats: &QueryStats,
    canister_id: CanisterId,
    num_nodes: usize,
    state: &mut ReplicatedState,
    logger: &ReplicaLogger,
) {
    // Note that the use of the number of nodes in the subnet like this does not handle the case that
    // the number of machines in the subnet might have changed throughout an epoch.
    // Given that subnet topology changes are an infrequent event, we tolerate this occasional inaccuracy here.
    let num_nodes = num_nodes as u128;
    if let Some(canister_state) = state.canister_state_make_mut(&canister_id) {
        let canister_query_stats = &mut canister_state.system_state.total_query_stats;
        canister_query_stats.num_calls += aggregated_stats.num_calls as u128 * num_nodes;
        canister_query_stats.num_instructions +=
            aggregated_stats.num_instructions as u128 * num_nodes;
        canister_query_stats.ingress_payload_size +=
            aggregated_stats.ingress_payload_size as u128 * num_nodes;
        canister_query_stats.egress_payload_size +=
            aggregated_stats.egress_payload_size as u128 * num_nodes;
    } else {
        info!(
            logger,
            "Received query stats for a canister {} which does not exist.", canister_id,
        );
    }
}
```

**File:** rs/query_stats/src/state_machine.rs (L307-311)
```rust
    // Check if we have enough nodes with reports to aggregate an epoch
    let need_stats_from = num_nodes.saturating_sub(get_faults_tolerated(num_nodes));
    if num_nodes_with_stats < need_stats_from {
        return false;
    }
```
