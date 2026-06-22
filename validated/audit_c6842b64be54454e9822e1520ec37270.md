### Title
QueryStats Per-Canister State Overwrite Without Accumulation in `process_payload` — (File: `rs/query_stats/src/state_machine.rs`)

---

### Summary

The `process_payload` function in `rs/query_stats/src/state_machine.rs` silently **overwrites** existing `QueryStats` for a `CanisterId` when a duplicate entry is received for the same `NodeId`/epoch, instead of **accumulating** them. The code acknowledges this as a bug via an `error!` log but takes no corrective action — the previous stats are discarded and replaced. This is the direct IC analog of the Suzaku `setRewardsAmountForEpochs` overwrite-without-guard vulnerability: a state-accumulation function that silently replaces prior values instead of adding to them.

---

### Finding Description

In `process_payload`, for each `CanisterQueryStats` message in the incoming `QueryStatsPayload`, the function inserts the stats into the per-node, per-epoch record:

```rust
// Insert the record into the state machine
let previous_record = stats.insert(message.canister_id, message.stats.clone());

// If there was a previous record, we have received a set of statistics twice, which is likely a bug
if previous_record.is_some() {
    error!(
        logger,
        "Received duplicate query stats for canister {} from same proposer {}.\
        This is a bug, possibly in the payload builder.",
        message.canister_id,
        query_stats.proposer
    );
}
``` [1](#0-0) 

`BTreeMap::insert` returns the old value and replaces it with the new one. When `previous_record.is_some()`, the prior stats for that canister are **silently discarded** and replaced by the new payload's stats. The correct behavior — consistent with how `QueryStats` are accumulated everywhere else in the codebase (e.g., `saturating_accumulate`) — would be to **add** the new stats to the existing ones.

The `QueryStatsCollector` correctly uses accumulation:

```rust
state
    .entry(canister_id)
    .or_default()
    .saturating_accumulate(stats);
``` [2](#0-1) 

And `apply_query_stats_to_canister` also accumulates into `TotalQueryStats`:

```rust
canister_query_stats.num_calls += aggregated_stats.num_calls as u128 * num_nodes;
canister_query_stats.num_instructions +=
    aggregated_stats.num_instructions as u128 * num_nodes;
``` [3](#0-2) 

Only `process_payload` breaks this invariant by using `insert` (overwrite) instead of `saturating_accumulate`.

---

### Impact Explanation

`QueryStats` feed into `TotalQueryStats` on each canister's `SystemState`, which is the authoritative record of query-call resource consumption used for billing and reporting. When a duplicate `CanisterId` entry causes an overwrite, the **previously recorded stats for that canister in that epoch are permanently lost** from the `RawQueryStats` aggregation input. The aggregated median computed in `try_aggregate_one_epoch` will then be based on an understated value for the affected node, causing the canister's `total_query_stats` to be **under-incremented**. This is a cycles/resource accounting bug: canisters are charged less than they consumed, and the accounting record is permanently incorrect. [4](#0-3) 

---

### Likelihood Explanation

The payload validator (`validate_payload_impl`) does check for duplicate `CanisterId`s within a single payload and against `previous_ids` derived from the certified state and `past_payloads`: [5](#0-4) 

However, `previous_ids` is assembled from two sources: (1) the certified state at `certified_height`, and (2) `past_payloads` (blocks proposed but not yet finalized in the current round). There is a window between when a block is **finalized** and when the state is **certified** during which a finalized block's stats may be absent from both sources. A malicious block proposer (operating below the consensus fault threshold — no subnet-majority corruption required) can exploit this window to propose a block whose `QueryStatsPayload` re-submits a `CanisterId` already present in a recently finalized but not-yet-certified block. The `process_payload` state machine will then silently overwrite the prior stats rather than reject or accumulate them.

Additionally, the comment in the code itself ("This is a bug, possibly in the payload builder") confirms the developers anticipated this path and chose only to log rather than guard against it. [6](#0-5) 

---

### Recommendation

Replace the overwriting `insert` with an accumulating entry update, consistent with the rest of the codebase:

```diff
- let previous_record = stats.insert(message.canister_id, message.stats.clone());
- if previous_record.is_some() {
-     error!(logger, "Received duplicate query stats ...");
- }
+ stats
+     .entry(message.canister_id)
+     .and_modify(|existing| existing.saturating_accumulate(&message.stats))
+     .or_insert_with(|| message.stats.clone());
```

This ensures that if a duplicate `CanisterId` arrives for the same proposer/epoch (whether due to a payload-builder bug or a malicious proposer exploiting the certification gap), the stats are accumulated rather than silently discarded.

---

### Proof of Concept

1. Node A proposes Block N containing `QueryStatsPayload { epoch: E, proposer: A, stats: [{ canister_id: C, num_instructions: 1000 }] }`. Block N is finalized but not yet certified.
2. Before the state at height N is certified, Node A proposes Block N+1 containing `QueryStatsPayload { epoch: E, proposer: A, stats: [{ canister_id: C, num_instructions: 500 }] }`.
3. The validator's `get_previous_ids` checks the certified state (which does not yet include Block N's stats) and `past_payloads` (which may not include Block N if it was finalized in a prior round). `CanisterId C` is not found in `previous_ids`, so the payload passes validation.
4. `deliver_query_stats` → `process_payload` is called for Block N+1. `stats.insert(C, { num_instructions: 500 })` overwrites the existing `{ num_instructions: 1000 }` entry. The 1000-instruction record is permanently lost.
5. When `try_aggregate_one_epoch` runs, Node A's contribution for canister C in epoch E is `500` instead of the correct `1500`, causing the median to be understated and `total_query_stats.num_instructions` to be under-incremented. [7](#0-6) [8](#0-7)

### Citations

**File:** rs/query_stats/src/state_machine.rs (L127-154)
```rust
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

**File:** rs/query_stats/src/state_machine.rs (L169-227)
```rust
fn process_payload(
    query_stats: &QueryStatsPayload,
    state: &mut ReplicatedState,
    logger: &ReplicaLogger,
    metrics: &QueryStatsAggregatorMetrics,
) -> bool {
    let state = &mut state.epoch_query_stats;

    // Check that we are not adding a payload for a height that has already been aggregated.
    if Some(query_stats.epoch) <= state.highest_aggregated_epoch {
        return false;
    }

    let node = state.stats.entry(query_stats.proposer).or_default();
    let stats = match node.last_key_value() {
        Some((highest_epoch, _)) => match highest_epoch.cmp(&query_stats.epoch) {
            // Add a new entry to the end of the records
            Ordering::Less => node.entry(query_stats.epoch).or_default(),
            // Get the last record
            Ordering::Equal => node.get_mut(&query_stats.epoch).unwrap(),
            // Node is trying to submit a record which should already be fully submitted
            Ordering::Greater => {
                error!(
                    logger,
                    "QueryStatsAggregator: Trying to add payload for epoch {:?} for proposer {:?}\
                    after already submitting values for {:?}. This is likely a bug in the payload builder.",
                    query_stats.epoch,
                    query_stats.proposer,
                    highest_epoch
                );
                return false;
            }
        },
        None => node.entry(query_stats.epoch).or_default(),
    };

    let mut query_stats_received = QueryStats::default();
    for message in &query_stats.stats {
        // Collect metrics about reveived statistics
        query_stats_received.saturating_accumulate(&message.stats);

        // Insert the record into the state machine
        let previous_record = stats.insert(message.canister_id, message.stats.clone());

        // If there was a previous record, we have received a set of statistics twice, which is likely a bug
        if previous_record.is_some() {
            error!(
                logger,
                "Received duplicate query stats for canister {} from same proposer {}.\
                This is a bug, possibly in the payload builder.",
                message.canister_id,
                query_stats.proposer
            );
        }
    }
    metrics.query_stats_received.add(&query_stats_received);

    true
}
```

**File:** rs/query_stats/src/lib.rs (L132-136)
```rust
        let mut state = self.current_query_stats.lock().unwrap();
        state
            .entry(canister_id)
            .or_default()
            .saturating_accumulate(stats);
```

**File:** rs/query_stats/src/payload_builder.rs (L223-303)
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
    }
```

**File:** rs/query_stats/src/payload_builder.rs (L339-378)
```rust
        // The query stats can be sent over multiple payloads
        // To not resend the same stats twice, we need to filter out the canister ids
        // we have already sent. It is imporant to only filter against canister ids if
        // the stats are not of a previous epoch
        let mut previous_ids = BTreeSet::<CanisterId>::new();

        // Check that the epoch we are requesting has not been aggregated yet
        // If there is no `highest_aggregated_epoch` in the state, we have not aggregated
        // any epochs, therefore we unwrap to `false`
        if state_stats
            .highest_aggregated_epoch
            .map(|highest_aggregated_epoch| epoch <= highest_aggregated_epoch)
            .unwrap_or(false)
        {
            warn!(
                every_n_seconds => 5,
                self.log,
                "QueryStats: requesting previous_ids for epoch {:?} that is below aggregated epoch {:?}",
                epoch,
                state_stats.highest_aggregated_epoch
            );

            return Err(invalid_artifact(
                InvalidQueryStatsPayloadReason::EpochAlreadyAggregated {
                    highest_aggregated_epoch: state_stats
                        .highest_aggregated_epoch
                        .unwrap_or(0.into()),
                    payload_epoch: epoch,
                },
            ));
        }

        // Check the certified state for stats that we have already sent
        let mut has_submitted_in_state = false;
        if let Some(state_stats) = get_stats_for_node_id_and_epoch(state_stats, &node_id, &epoch)
            .inspect(|_| has_submitted_in_state = true)
            .map(|record| record.keys())
        {
            previous_ids.extend(state_stats);
        }
```
