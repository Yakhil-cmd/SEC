Audit Report

## Title
Missing Total Memory-Size Check in `validate_ingress_payload` When `hashes_in_blocks` Is Enabled — (`rs/ingress_manager/src/ingress_selector.rs`)

## Summary

`validate_ingress_payload` computes `size_estimates.memory` via `payload_size_estimates` but discards it, returning only `size_estimates.wire` with no comparison against `max_ingress_bytes_per_block`. When `HASHES_IN_BLOCKS_ENABLED = true`, the wire size of a payload is only the sum of message IDs (~32 bytes each), so a payload containing 1000 × 2 MB messages passes every validation guard with a ~32 KB wire footprint while its in-memory reconstruction is ~2 GB — 500× the intended 4 MB limit. This asymmetry with `get_ingress_payload`, which enforces both limits, is confirmed by the production code.

## Finding Description

`get_ingress_payload` enforces both wire and memory limits per message:

```rust
// rs/ingress_manager/src/ingress_selector.rs lines 212-220
if accumulated_wire_size + size_estimates.wire > wire_byte_limit { break 'outer; }
if accumulated_memory_size + size_estimates.memory > memory_byte_limit { break 'outer; }
```

`validate_ingress_payload` calls `payload_size_estimates` which correctly computes both `memory_bytes` and `wire_bytes`:

```rust
// lines 644-658
fn payload_size_estimates(&self, payload: &IngressPayload) -> SizeEstimates {
    let memory_bytes = payload.total_messages_size_estimate() + payload.total_ids_size_estimate();
    let wire_bytes = if self.hashes_in_blocks_enabled() {
        payload.total_ids_size_estimate()   // IDs only
    } else { memory_bytes };
    SizeEstimates { memory: memory_bytes, wire: wire_bytes }
}
```

But `validate_ingress_payload` silently discards `memory` and returns only `wire`:

```rust
// lines 420-422
let size_estimates = self.payload_size_estimates(payload);
Ok(size_estimates.wire)   // memory never checked
```

The three checks that do exist in `validate_ingress_payload` are all insufficient:
1. `payload.message_count() > settings.max_ingress_messages_per_block` (line 373) — passes at exactly 1000 messages.
2. Per-message `ingress_message_size > settings.max_ingress_bytes_per_message` (line 501) — each 2 MB message individually passes.
3. Upstream `PayloadBuilderImpl::validate_payload` (payload_builder.rs lines 144-168) accumulates the wire sizes returned by each section and hard-rejects only above `max_block_payload_size * 2 = 8 MB`. With `hashes_in_blocks_enabled`, the ingress section returns ~32 KB for 1000 messages, far below 8 MB.

A Byzantine block maker does not use `get_ingress_payload`; it directly constructs an `IngressPayload` with all 1000 messages. The block is stripped before broadcast (wire size ~32 KB). Receiving honest replicas reconstruct the full block by fetching all 1000 messages from the pool (2 GB total), then call `validate_ingress_payload`, which passes. The 2 GB allocation persists through execution.

The ingress pool imposes no hard cap: `ingress_pool_max_count: usize::MAX` and `ingress_pool_max_bytes: usize::MAX` (artifact_pool.rs lines 38-39), so all 1000 messages propagate freely to honest replicas' pools.

## Impact Explanation

This is a **High** severity finding matching: *"Application/platform-level DoS, crash, consensus blocking, certified-state disruption, or subnet availability impact not based on raw volumetric DDoS."*

A single Byzantine block maker below the consensus fault threshold can force every honest replica to allocate ~2 GB of memory per crafted block. Under concurrent load or on replicas with constrained memory, this causes OOM or severe memory pressure, stalling consensus on affected nodes. If a sufficient number of honest replicas are affected simultaneously, the subnet halts. The `MAX_INGRESS_BYTES_PER_BLOCK` constant is explicitly documented as a memory-usage guard for replicas; this finding demonstrates it is not enforced on the validation path.

## Likelihood Explanation

- **Attacker role**: A single Byzantine replica below the fault threshold — within scope.
- **Precondition**: 1000 valid, signed, non-expired ingress messages of ~2 MB each must be present in honest replicas' ingress pools. Each message passes the per-message HTTP endpoint check (`≤ MAX_INGRESS_BYTES_PER_MESSAGE_APP_SUBNET = 2 MB`) and `validate_ingress_pool_object`. Pool admission is effectively unbounded (`usize::MAX`).
- **Cost**: Submitting ~2 GB of ingress data is expensive in cycles and bandwidth, placing this in the Medium-to-High feasibility range for a well-resourced attacker.
- **Repeatability**: The attack is deterministic and repeatable every consensus round the Byzantine node is block maker.

## Recommendation

Add a total-memory check inside `validate_ingress_payload` immediately after computing `size_estimates`, using `max_ingress_bytes_per_block` already available from `settings`:

```rust
let size_estimates = self.payload_size_estimates(payload);
let memory_limit = NumBytes::new(settings.max_ingress_bytes_per_block as u64);
if size_estimates.memory > memory_limit {
    return Err(ValidationError::InvalidArtifact(
        InvalidIngressPayloadReason::IngressPayloadTooLarge(
            size_estimates.memory.get() as usize,
            settings.max_ingress_bytes_per_block,
        ),
    ));
}
Ok(size_estimates.wire)
```

This mirrors the guard already present in `get_ingress_payload` and closes the asymmetry between building and validating.

## Proof of Concept

```
1. Byzantine replica submits 1000 SignedIngress messages, each with a
   ~2 MB method_payload. Each passes the HTTP endpoint size check
   (≤ MAX_INGRESS_BYTES_PER_MESSAGE_APP_SUBNET = 2 MB).

2. Messages propagate to honest replicas' ingress pools
   (ingress_pool_max_count = usize::MAX, no hard cap).

3. Byzantine block maker directly constructs an IngressPayload
   containing all 1000 messages (bypassing get_ingress_payload).

4. Block is stripped before broadcast:
     wire_size = 1000 × EXPECTED_MESSAGE_ID_LENGTH ≈ 32 KB

5. Receiving honest replicas receive StrippedBlockProposal, fetch all
   1000 full messages (2 GB total), assemble the block.

6. validate_ingress_payload is called:
     - message_count = 1000 ≤ 1000                ✓  (line 373)
     - each message ≤ 2 MB                         ✓  (line 501)
     - wire size ≈ 32 KB, returned to caller       ✓  (line 422)
     - memory = 2 GB — NOT CHECKED                 ✓  (passes)

7. Block is accepted. 2 GB remains allocated. Replica OOMs or
   experiences severe memory pressure, stalling consensus.

Deterministic invariant test assertion:
  let payload = build_ingress_payload_with_n_messages(1000, 2 * MB);
  assert!(validate_ingress_payload(&payload, ...).is_ok());
  // passes today — should return Err(IngressPayloadTooLarge)
```