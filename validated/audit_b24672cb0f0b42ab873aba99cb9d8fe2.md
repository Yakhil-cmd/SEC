Audit Report

## Title
Unbounded Canister ID Accumulation in `try_aggregate_one_epoch` Enables Synchronous Aggregation DoS — (File: `rs/query_stats/src/state_machine.rs`)

## Summary

`validate_payload_impl` in `rs/query_stats/src/payload_builder.rs` enforces no cap on the total number of unique `CanisterId` values a single node may report across all blocks within one epoch. A malicious block proposer can craft disjoint sets of fabricated canister IDs in each block it proposes, accumulating an arbitrarily large record in `RawQueryStats`. When the honest 2/3 quorum triggers `try_aggregate_one_epoch`, the function iterates over every accumulated entry synchronously during batch delivery — native Rust with no instruction-counter timeout — on every replica simultaneously, creating a realistic path to consensus stall.

## Finding Description

**Accumulation path**: `validate_payload_impl` (`rs/query_stats/src/payload_builder.rs`, L223–303) checks that the proposer node ID matches, the epoch is not too high, there are no duplicates within a single payload, and no IDs already present in past payloads for the same node/epoch. There is no check on the *cumulative* count of unique canister IDs a node has reported across all blocks in the epoch. A malicious proposer submits a fresh disjoint batch of fabricated IDs in every block it proposes; each batch passes the `previous_ids` check because the sets are disjoint.

`process_payload` (`rs/query_stats/src/state_machine.rs`, L169–227) inserts each entry directly into `state.stats[proposer][epoch][canister_id]` with no size guard. `RawQueryStats` (`rs/types/types/src/batch/execution_environment.rs`, L136–140) is a plain nested `BTreeMap` with no capacity limit.

**Aggregation path**: `deliver_query_stats` (`rs/query_stats/src/state_machine.rs`, L419–437) is called synchronously from `StateMachineImpl::execute_round` (`rs/messaging/src/state_machine.rs`, L164–171) during deterministic batch execution. Inside `try_aggregate_one_epoch` (L239–374), once the 2/3 quorum is met, the function:

1. Builds a `BTreeMap<CanisterId, Vec<&QueryStats>>` by flattening all nodes' records — O(M log M) insertions (L317–321).
2. Iterates over every unique `CanisterId`, calling `aggregate_query_stats` (sort + median) and `apply_query_stats_to_canister` for each (L340–368).

`apply_query_stats_to_canister` silently skips non-existent canisters (L148–153), so fabricated IDs impose full iteration cost with no useful work. There is no DTS mechanism for this native Rust code path; the entire call must complete atomically before the replica can advance to the next batch.

**Existing checks are insufficient**: The per-payload byte limit (`serialize_with_limit`, `rs/types/types/src/batch/execution_environment.rs`, L245–284) limits entries per block but not the cumulative total across blocks. The `previous_ids` deduplication check prevents re-reporting the same ID in the same epoch but explicitly enables accumulation of new IDs across blocks.

## Impact Explanation

`deliver_query_stats` executes synchronously in the message-routing layer on every replica. Processing hundreds of thousands to millions of fabricated canister IDs — O(M log M) BTreeMap insertions plus O(M) iteration with per-entry work — can consume seconds of wall-clock time. Because all replicas must execute the same deterministic batch before advancing, a sufficiently large M stalls the entire subnet simultaneously. This matches the allowed impact: **High — Application/platform-level DoS, consensus blocking, or subnet availability impact not based on raw volumetric DDoS** ($2,000–$10,000).

## Likelihood Explanation

The attacker requires only a single subnet node that participates in block proposal — a role that rotates to every node under IC's round-robin consensus. No threshold corruption, admin key, or social engineering is needed. In a 13-node subnet the malicious node proposes approximately 600/13 ≈ 46 blocks per epoch; at ~25,000 fabricated IDs per block (within a 1 MB payload budget at ~40 bytes/entry) this yields ~1.15 million unique IDs per epoch. For smaller subnets (e.g., 4 nodes) the figure reaches ~3.75 million. The attack is passive, repeatable every epoch, and requires no victim interaction.

## Recommendation

1. **Enforce a per-node-per-epoch canister ID cap in `validate_payload_impl`**: Reject any payload whose inclusion would push the reporting node's cumulative unique canister ID count for the epoch above a safe constant (e.g., 10,000). The `get_previous_ids` helper already computes the current count; add a check against a configured maximum before returning `Ok(())`.

2. **Cap `RawQueryStats` growth in `process_payload`**: Before inserting entries, check the current size of `state.stats[proposer][epoch]` and drop excess entries, analogous to how `serialize_with_limit` drops trailing stats at build time.

3. **Validate canister IDs against certified state during payload validation**: Reject stats for canister IDs absent from the replicated state at the certified height, eliminating the fabricated-ID vector entirely.

## Proof of Concept

A deterministic integration test or PocketIC scenario:

1. Create a subnet with N = 4 nodes.
2. Designate node 1 as malicious. For each block node 1 proposes within epoch 0 (approximately 600/4 = 150 blocks), craft a `QueryStatsPayload` containing 25,000 distinct fabricated `CanisterId` values not present in any prior payload from node 1 in this epoch. Each payload serializes within the 1 MB block budget.
3. Have nodes 2, 3, 4 submit normal (empty or minimal) payloads and advance to epoch 1, triggering the 2/3 quorum.
4. Observe that `try_aggregate_one_epoch` is called with ~3.75 million entries in the aggregation map. Measure wall-clock time; assert it exceeds an acceptable per-batch budget (e.g., 500 ms), demonstrating the stall condition.
5. Confirm the attack is repeatable each epoch with a fresh set of fabricated IDs.