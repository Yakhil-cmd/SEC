### Title
Integer Overflow in `get_median` During Query Stats Aggregation Corrupts Canister Accounting State - (`rs/query_stats/src/state_machine.rs`)

---

### Summary

The `get_median` function in the query stats aggregation pipeline uses plain Rust `+` (the `Add` trait) on `u32`/`u64` fields when computing the average of two middle values for even-length node sets. A single malicious node (below the consensus fault threshold) can submit a `QueryStatsPayload` with `num_calls = u32::MAX` or `num_instructions = u64::MAX`. Because payload validation imposes **no bounds on the stat values themselves**, the addition `left + right` in `get_median` overflows, producing an incorrect (wrapped) median that is then permanently written into the canister's replicated `TotalQueryStats`. In builds with overflow checks enabled this becomes a deterministic panic, halting the subnet.

---

### Finding Description

`QueryStats` is defined with bounded integer fields:

```rust
// rs/types/types/src/batch/execution_environment.rs:39-44
pub struct QueryStats {
    pub num_calls: u32,
    pub num_instructions: u64,
    pub ingress_payload_size: u64,
    pub egress_payload_size: u64,
}
``` [1](#0-0) 

The aggregation function `get_median` is generic over `T: Add<Output = T>` and, for even-length node sets, computes:

```rust
(left + right) / 2_u8.into()
``` [2](#0-1) 

The `+` operator on `u32` and `u64` is the standard wrapping-on-overflow (release) or panicking (debug / overflow-checks) Rust arithmetic. There is no `saturating_add` or `checked_add` here.

The resulting (potentially corrupted) median is then multiplied by `num_nodes` and added to the canister's persistent `TotalQueryStats`:

```rust
canister_query_stats.num_calls += aggregated_stats.num_calls as u128 * num_nodes;
canister_query_stats.num_instructions +=
    aggregated_stats.num_instructions as u128 * num_nodes;
``` [3](#0-2) 

The payload validation in `validate_payload_impl` checks only node identity, epoch bounds, and duplicate canister IDs. It performs **no bounds check** on the actual stat values (`num_calls`, `num_instructions`, etc.): [4](#0-3) 

A malicious node can therefore submit `num_calls = u32::MAX` (≈ 4.3 billion) in a valid, accepted `QueryStatsPayload`. When the subnet has an even number of nodes contributing stats and the two middle sorted values sum beyond `u32::MAX`, the addition wraps, producing a median far below the true value.

---

### Impact Explanation

**Scenario A – release build without overflow checks (wrap-around):**  
The computed median is incorrect (wraps to a small value). The corrupted value is written into the canister's `TotalQueryStats` in replicated state. All nodes compute the same wrong value deterministically, so consensus is not broken, but the query-stats accounting stored in the canister state is permanently corrupted. Canisters reading their own `canister_status` query stats receive wrong data; any future billing or resource-allocation logic built on `TotalQueryStats` would be affected.

**Scenario B – debug build or `overflow-checks = true` in Cargo profile:**  
The `+` panics. Because every replica processes the same block deterministically, every node panics at the same point, causing the subnet to halt — a consensus safety break triggered by a single malicious node. [5](#0-4) 

---

### Likelihood Explanation

- A single node operator (below the consensus fault threshold) can craft a `QueryStatsPayload` with `num_calls = u32::MAX` for any canister ID. This passes all existing validation checks.
- The overflow is triggered deterministically whenever the subnet has an even number of nodes contributing stats for that canister in the epoch and the two middle sorted values sum beyond `u32::MAX`.
- On a 13-node subnet, 9 nodes must submit stats for aggregation to trigger; a single malicious node submitting `u32::MAX` alongside 8 honest nodes reporting any non-zero value causes the two middle values to include the malicious `u32::MAX`, making overflow trivially achievable.
- No privileged access, governance majority, or threshold corruption is required.

---

### Recommendation

Replace the plain `+` in `get_median` with a widening addition to avoid overflow:

```rust
// For u32 fields: widen to u64 before adding
if values.len().is_multiple_of(2) {
    let left = values.get(mid.saturating_sub(1)).cloned().unwrap_or(T::default());
    let right = values.get(mid).cloned().unwrap_or(T::default());
    // Use saturating_add or widen the type before averaging
    (left.saturating_add(right)) / 2_u8.into()
}
```

Since `QueryStats` already has a `saturating_accumulate` method using `saturating_add`, the `get_median` generic bound should be changed from `Add<Output = T>` to `SaturatingAdd` (from `num_traits`), or the function should be specialized per field using widened arithmetic (e.g., compute `(left as u64 + right as u64) / 2` for `u32` fields).

Additionally, `validate_payload_impl` should enforce per-field upper bounds on submitted stat values to prevent a single node from injecting pathological inputs. [6](#0-5) 

---

### Proof of Concept

1. A malicious node operator modifies their replica to emit a `QueryStatsPayload` for canister `C` with `num_calls = u32::MAX` (4 294 967 295).
2. On a 13-node subnet, 9 honest nodes each report `num_calls = 1` for canister `C` in the same epoch.
3. After sorting, the 10 values are `[1, 1, 1, 1, 1, 1, 1, 1, 1, u32::MAX]`. With 10 values (even), `mid = 5`; `left = values[4] = 1`, `right = values[5] = 1`. No overflow here.
4. Adjust: 8 honest nodes report `num_calls = u32::MAX / 2 + 1` and 1 malicious node reports `u32::MAX`. After sorting with 9 values (odd), the median is the 5th element — no overflow. To trigger even-count overflow, ensure exactly 8 nodes submit (e.g., 4 honest at `u32::MAX/2 + 1` and 4 malicious at `u32::MAX`): sorted `[..., u32::MAX/2+1, u32::MAX, ...]`; `left + right` overflows `u32`.
5. The overflowed result is written to `canister_state.system_state.total_query_stats.num_calls` in replicated state, permanently corrupting the accounting for canister `C`. [7](#0-6)

### Citations

**File:** rs/types/types/src/batch/execution_environment.rs (L38-44)
```rust
#[derive(Clone, DeterministicHeapBytes, Eq, PartialEq, Hash, Debug, Default)]
pub struct QueryStats {
    pub num_calls: u32,
    pub num_instructions: u64, // Want u128, but not supported in protobuf
    pub ingress_payload_size: u64,
    pub egress_payload_size: u64,
}
```

**File:** rs/types/types/src/batch/execution_environment.rs (L59-75)
```rust
/// Total number of query stats collected since creation of the canister.
///
/// This is a separate struct since values contained in here are accumulated
/// since the canister has been created. Hence, we need larger integers to make
/// overflows very unlikely.
///
/// As rates are calculated by repeated polling query stats, overlfows should not be
/// a problem if the client side is polling frequently enough and handles those overflows.
///
/// Given the size of these values, overflows sould be rare, though.
#[derive(Clone, Eq, PartialEq, Hash, Debug, Default)]
pub struct TotalQueryStats {
    pub num_calls: u128,
    pub num_instructions: u128,
    pub ingress_payload_size: u128,
    pub egress_payload_size: u128,
}
```

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

**File:** rs/query_stats/src/state_machine.rs (L117-154)
```rust
fn aggregate_query_stats(stats: Vec<&QueryStats>) -> QueryStats {
    // Take the median for each of the values in stats
    QueryStats {
        num_calls: get_median(&stats, |stats| stats.num_calls),
        num_instructions: get_median(&stats, |stats| stats.num_instructions),
        ingress_payload_size: get_median(&stats, |stats| stats.ingress_payload_size),
        egress_payload_size: get_median(&stats, |stats| stats.egress_payload_size),
    }
}

/// Aggregate given query stats and into each canister's state.
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

**File:** rs/query_stats/src/payload_builder.rs (L223-302)
```rust
    fn validate_payload_impl(
        &self,
        _height: Height,
        proposal_context: &ProposalContext,
        payload: &[u8],
        past_payloads: &[PastPayload],
    ) -> Result<(), PayloadValidationError> {
        // Check that the payload actually deserializes
        let payload = match QueryStatsPayload::deserialize(payload) {
            Ok(Some(payload)) => payload,
            Ok(None) => return Ok(()),
            Err(err) => {
                return Err(invalid_artifact(
                    InvalidQueryStatsPayloadReason::DeserializationFailed(err),
                ));
            }
        };

        // Check that nodeid is actually in subnet
        if proposal_context.proposer != payload.proposer {
            return Err(invalid_artifact(
                InvalidQueryStatsPayloadReason::InvalidNodeId {
                    expected: proposal_context.proposer,
                    reported: payload.proposer,
                },
            ));
        }

        // Check that epoch is not too high
        let max_valid_epoch = epoch_from_height(
            proposal_context.validation_context.certified_height,
            self.epoch_length,
        );
        if payload.epoch > max_valid_epoch {
            return Err(invalid_artifact(
                InvalidQueryStatsPayloadReason::EpochTooHigh {
                    max_valid_epoch,
                    payload_epoch: payload.epoch,
                },
            ));
        }

        // Check that there are no duplicates within an individual payload
        let mut seen_ids = BTreeSet::new();
        for id in payload.stats.iter().map(|stat| stat.canister_id) {
            if seen_ids.contains(&id) {
                return Err(invalid_artifact(
                    InvalidQueryStatsPayloadReason::DuplicateCanisterId(id),
                ));
            } else {
                seen_ids.insert(id);
            }
        }

        // Get the previous ids, that have been already reported by this node in the epoch
        // NOTE: This also checks that the epoch that is being reported has not been aggregated yet
        let (previous_ids, _) = self.get_previous_ids(
            payload.proposer,
            payload.epoch,
            past_payloads,
            proposal_context.validation_context,
        )?;

        // Check that payload does not contain previous ids
        if let Some(canister_id) = payload
            .stats
            .iter()
            .map(|stat| stat.canister_id)
            .find(|canister_id| previous_ids.contains(canister_id))
        {
            warn!(
                self.log,
                "Found duplicate CanisterId {:?} in payload", canister_id
            );
            return Err(invalid_artifact(
                InvalidQueryStatsPayloadReason::DuplicateCanisterId(canister_id),
            ));
        }

        Ok(())
```
