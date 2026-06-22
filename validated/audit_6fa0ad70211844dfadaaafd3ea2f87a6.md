### Title
Unbounded Canister ID Accumulation in `try_aggregate_one_epoch` Enables Consensus-Stalling DoS — (File: `rs/query_stats/src/state_machine.rs`)

---

### Summary

The `try_aggregate_one_epoch` function in the QueryStats replicated state machine iterates over every unique `CanisterId` reported by every node in an epoch with no upper bound on that count. A malicious block proposer can fill every block it proposes with maximum-size query-stats payloads that each contain as many distinct (possibly non-existent) canister IDs as the byte limit allows. Because `deliver_query_stats` is called **synchronously** during batch delivery on every replica, the resulting O(M) aggregation work — where M can reach millions of unique canister IDs — is imposed on the entire subnet simultaneously, creating a realistic path to consensus stall.

---

### Finding Description

**Accumulation path (cheap, per-block)**

Each block proposer calls `QueryStatsPayloadBuilderImpl::build_payload_impl`, which serialises the node's locally collected stats into a byte-limited payload and drops trailing entries if the limit is exceeded. [1](#0-0) 

The payload validator (`validate_payload_impl`) enforces:
- The proposer node ID matches the block signer.
- The epoch is not too high.
- No duplicate `CanisterId` within a single payload.
- No `CanisterId` already present in past payloads for the same node/epoch. [2](#0-1) 

**What is NOT validated**: there is no cap on the *total* number of unique canister IDs a node may report across all blocks in one epoch. A malicious proposer can report a fresh batch of unique IDs in every block it proposes, accumulating an arbitrarily large set bounded only by `epoch_length × max_query_stats_bytes_per_block / per_entry_size`.

**Aggregation path (expensive, single call)**

When `deliver_query_stats` is called during batch delivery it invokes `try_aggregate_one_epoch` in a loop of up to 100 iterations: [3](#0-2) 

Inside `try_aggregate_one_epoch`, once the 2/3-quorum threshold is met, the function:

1. Builds a `BTreeMap<CanisterId, Vec<&QueryStats>>` by flattening **all** nodes' records for the epoch — O(N × M) insertions.
2. Iterates over every unique `CanisterId` in that map, calling `aggregate_query_stats` (sort + median) and `apply_query_stats_to_canister` for each. [4](#0-3) 

`apply_query_stats_to_canister` silently skips non-existent canisters, so the attacker does not need to know which canisters are actually installed: [5](#0-4) 

**Asymmetry**: accumulation costs O(1) per block; aggregation costs O(M) in a single synchronous call on every replica.

**`RawQueryStats` state structure** has no size cap: [6](#0-5) 

---

### Impact Explanation

`deliver_query_stats` is invoked from the message-routing layer during deterministic batch execution — native Rust code with no instruction-counter timeout. Every replica in the subnet must execute the same aggregation before it can advance to the next batch. If the aggregation over millions of unique canister IDs takes seconds, **all** replicas stall simultaneously, blocking consensus progress. Unlike canister Wasm execution, there is no DTS (Deterministic Time Slicing) mechanism to spread this work across rounds; the entire `try_aggregate_one_epoch` call must complete atomically within one batch-delivery step.

---

### Likelihood Explanation

- The attacker needs only a single malicious node that is a block proposer — a role that rotates to every node regularly under the IC's round-robin consensus.
- No threshold corruption, admin key, or social engineering is required.
- The attack is passive: the malicious node simply fills its query-stats payload slot with unique (fabricated) canister IDs in every block it proposes.
- Honest nodes' 2/3-quorum submission is what *triggers* the expensive aggregation; the malicious node's oversized record is then included in the aggregation automatically.

---

### Recommendation

1. **Enforce a per-node-per-epoch canister ID cap** in `validate_payload_impl`. Reject any payload whose inclusion would push the reporting node's total unique canister IDs for the epoch above a safe constant (e.g., 10 000). [7](#0-6) 

2. **Cap the total unique canister IDs processed per `try_aggregate_one_epoch` call** and defer excess entries to the next epoch, analogous to how `serialize_with_limit` drops trailing stats at build time. [8](#0-7) 

3. **Validate canister IDs against the certified state** during payload validation, rejecting stats for canister IDs that do not exist in the replicated state at the certified height.

---

### Proof of Concept

A malicious node acting as block proposer executes the following strategy each time it proposes a block within an epoch:

```
epoch_length          = QUERY_STATS_EPOCH_LENGTH  (e.g. 600 blocks)
max_qs_bytes_per_block ≈ 1 MB  (remaining block budget after other payloads)
per_entry_bytes        ≈ 40 B  (CanisterId ~10 B + 4 × u64 stats fields)

unique_ids_per_block   = 1_000_000 / 40  ≈ 25_000
unique_ids_per_epoch   = 25_000 × 600   = 15_000_000
```

The malicious node generates 15 million distinct (fabricated) `CanisterId` values, reporting a fresh 25 000-entry batch in each block it proposes. The `previous_ids` deduplication check passes because each block uses a disjoint set of IDs.

When the honest 2/3 quorum is reached, `try_aggregate_one_epoch` must:
- Insert 15 million entries into a `BTreeMap` — O(15M × log 15M) ≈ 360M comparisons.
- Call `aggregate_query_stats` + `apply_query_stats_to_canister` for each of the 15 million entries.

This work is performed synchronously on every replica during batch delivery, with no timeout, stalling the entire subnet until the loop completes. [9](#0-8) [7](#0-6)

### Citations

**File:** rs/query_stats/src/payload_builder.rs (L219-221)
```rust
        // Serialize the payload, drop messages at the end if necessary
        payload.serialize_with_limit(max_size)
    }
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

**File:** rs/query_stats/src/state_machine.rs (L239-374)
```rust
fn try_aggregate_one_epoch(
    replicated_state: &mut ReplicatedState,
    logger: &ReplicaLogger,
    metrics: &QueryStatsAggregatorMetrics,
) -> bool {
    // For the aggregation to work correctly, we need to remove all entries from epochs equal or
    // below current `highest_aggregated_epoch`.
    purge_records(replicated_state);

    // Get the number of nodes of this subnet
    let num_nodes = replicated_state.system_metadata().own_subnet_size();
    debug_assert!(num_nodes.is_some());
    let Some(num_nodes) = num_nodes else {
        metrics.query_stats_critical_error_aggregator_failure.inc();
        error!(
            logger,
            "{}: QueryStats Aggregator: Failed to get own subnet size",
            CRITICAL_ERROR_AGGREGATION_FAILURE
        );
        return false;
    };

    let state = &mut replicated_state.epoch_query_stats;

    // Get the next epoch that we want to aggregate
    // Usually this is `highest_aggregated_epoch + 1`, but occasionally there might be
    // large gaps in between (e.g. the feature was deactivated for a while).
    // If we checked each `highest_aggregated_epoch + 1`, we would aggregate a lot of 0 epochs
    // Instead, we check for the lowest epoch that any node has stored as the `next_epoch`.
    let Some(&next_epoch) = state
        .stats
        .values()
        .filter_map(|records| records.first_key_value())
        .map(|(epoch, _stats)| epoch)
        .min()
    else {
        return false;
    };

    // Get the aggregatable records from the different `node_id`s
    let mut num_nodes_with_stats = 0;
    let mut aggregatable_records = vec![];
    for records in state.stats.values() {
        match records.len() {
            // If there are no records at all, this node has no stats to contribute
            0 => (),
            // If there is only one record and it's the one for a higher epoch, we know that this node has empty stats for
            // the current round. If the epoch is the current epoch, we don't know if the node has already fully
            // commited the record.
            1 => {
                let (epoch, _) = records.first_key_value().unwrap();
                if *epoch > next_epoch {
                    num_nodes_with_stats += 1;
                }
            }
            // If we have 2 or more records we know that the data is aggregatable.
            // We still need to check, whether the first record actually points to the epoch we care about.
            // Otherwise this node has empty stats to report for the current epoch.
            2.. => {
                num_nodes_with_stats += 1;
                let (epoch, stats) = records.first_key_value().unwrap();
                if *epoch == next_epoch {
                    aggregatable_records.push(stats)
                }
            }
        }
    }

    // Check if we have enough nodes with reports to aggregate an epoch
    let need_stats_from = num_nodes.saturating_sub(get_faults_tolerated(num_nodes));
    if num_nodes_with_stats < need_stats_from {
        return false;
    }

    // Increase the highest aggregated epoch
    state.highest_aggregated_epoch = Some(next_epoch);

    // We have an iterator over maps but we want a map over iterators
    let mut records: BTreeMap<CanisterId, Vec<_>> = BTreeMap::new();
    aggregatable_records
        .iter()
        .flat_map(|inner| inner.iter())
        .for_each(|(&canister_id, stat)| records.entry(canister_id).or_default().push(stat));

    info!(
        logger,
        "QueryStats aggregation summary: num_nodes: {}, need_stats_from: {}, \
            num_nodes_with_stats: {}, aggregatable_records: {}, aggregatable_canisters: {}",
        num_nodes,
        need_stats_from,
        num_nodes_with_stats,
        aggregatable_records.len(),
        records.len(),
    );

    // Aggregate statistics
    let mut empty_stats_counter: usize = 0;
    let mut total_stats_counter: usize = 0;

    let empty_stats = QueryStats::default();
    let mut query_stats_to_be_applied = vec![];
    for (canister_id, mut stats) in records {
        let num_empty_stats = num_nodes_with_stats.saturating_sub(stats.len());
        stats.append(&mut vec![&empty_stats; num_empty_stats]);

        empty_stats_counter += num_empty_stats;
        total_stats_counter += stats.len();

        let aggregated_stats = aggregate_query_stats(stats);
        query_stats_to_be_applied.push((canister_id, aggregated_stats));
    }

    metrics
        .query_stats_empty_stats_aggregated
        .add(empty_stats_counter as i64);
    metrics
        .query_stats_total_aggregated
        .add(total_stats_counter as i64);

    let mut delivered_query_stats = QueryStats::default();
    for (canister_id, aggregated_stats) in query_stats_to_be_applied {
        delivered_query_stats.saturating_accumulate(&aggregated_stats);

        apply_query_stats_to_canister(
            &aggregated_stats,
            canister_id,
            num_nodes,
            replicated_state,
            logger,
        );
    }

    metrics.query_stats_delivered.add(&delivered_query_stats);

    true
}
```

**File:** rs/query_stats/src/state_machine.rs (L419-437)
```rust
pub fn deliver_query_stats(
    query_stats: &QueryStatsPayload,
    state: &mut ReplicatedState,
    logger: &ReplicaLogger,
    metrics: &QueryStatsAggregatorMetrics,
) {
    if process_payload(query_stats, state, logger, metrics) {
        // While in theory is is guaranteed that `try_aggregate_one_epoch` will eventually return
        // `false`, the code is relatively complex and we don't want to rely on correct implementation
        // only.
        for _ in 0..100 {
            if !try_aggregate_one_epoch(state, logger, metrics) {
                break;
            }
        }

        update_metrics(state, metrics)
    }
}
```

**File:** rs/types/types/src/batch/execution_environment.rs (L136-140)
```rust
#[derive(Clone, Eq, PartialEq, Hash, Debug, Default)]
pub struct RawQueryStats {
    pub highest_aggregated_epoch: Option<QueryStatsEpoch>,
    pub stats: BTreeMap<NodeId, BTreeMap<QueryStatsEpoch, BTreeMap<CanisterId, QueryStats>>>,
}
```
