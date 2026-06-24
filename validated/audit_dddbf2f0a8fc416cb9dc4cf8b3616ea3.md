The code evidence is sufficient to render a verdict. Let me verify one final detail about the `try_assemble` flow.

Audit Report

## Title
Missing Memory-Size Bound in `validate_ingress_payload` Enables OOM via `hashes_in_blocks` Amplification — (`rs/ingress_manager/src/ingress_selector.rs`)

## Summary

When `hashes_in_blocks` is enabled (hardcoded `true` in production), `validate_ingress_payload` enforces only the wire-size (hash IDs, ~32 bytes each) of an ingress payload, not its in-memory size. A Byzantine block proposer below the consensus fault threshold can craft a block containing up to 1000 × 2 MB = 2 GB of ingress data. The stripped block passes all wire-size checks (~32 KB), but receiving replicas allocate up to 2 GB during block reconstruction — before validation runs — potentially causing OOM and consensus stall.

## Finding Description

**`HASHES_IN_BLOCKS_ENABLED` is hardcoded `true` in production:** [1](#0-0) 

**`validate_ingress_payload` discards the `memory` field entirely:**

After the per-message loop, `validate_ingress_payload` computes size estimates but returns only `size_estimates.wire`, with no check of `size_estimates.memory` against `max_ingress_bytes_per_block`: [2](#0-1) 

**`payload_size_estimates` correctly computes both fields, but only `wire` is used in validation:**

When `hashes_in_blocks` is enabled, `wire` is only the sum of hash IDs (~32 bytes each), while `memory` is the full message size: [3](#0-2) 

**The consensus-level `validate_payload` accumulates only the returned wire size:** [4](#0-3) 

With 1000 × 32-byte hashes = 32 KB wire size, the 4 MB × 2 = 8 MB hard rejection threshold is trivially satisfied.

**Contrast with `get_ingress_payload` (honest path), which enforces both limits:** [5](#0-4) 

And the final serialized payload is also double-checked against both limits before returning: [6](#0-5) 

**Per-message size is bounded but total is not:**

`validate_ingress` checks each individual message against `max_ingress_bytes_per_message` (2 MB): [7](#0-6) 

So 1000 messages × 2 MB each = 2 GB all individually pass. The aggregate memory bound (`MAX_INGRESS_BYTES_PER_BLOCK = 4 MB`) is never enforced during validation: [8](#0-7) 

**No size guard in the assembler reconstruction path:**

`try_reconstruct_payload` collects all fetched messages with no accumulated-size check before building the `IngressPayload`: [9](#0-8) 

`try_insert` similarly has no size guard: [10](#0-9) 

The `assemble_message` loop spawns concurrent fetch tasks for all 1000 missing messages and accumulates them in memory before `try_assemble` is called — which is before `validate_ingress_payload` is ever invoked: [11](#0-10) 

## Impact Explanation

This is a **High** severity finding matching the allowed impact: *"Application/platform-level DoS, crash, consensus blocking, certified-state disruption, or subnet availability impact not based on raw volumetric DDoS."*

A Byzantine block proposer can force receiving replicas to allocate ~2 GB of memory per malicious block proposal during reconstruction, before any validation check can reject it. If a quorum of honest replicas crash or are memory-starved, the subnet loses the ability to finalize blocks and halts. The attack is not volumetric DDoS — it exploits a protocol-level asymmetry between wire size and memory size introduced by the `hashes_in_blocks` feature.

## Likelihood Explanation

The attacker must be a valid replica below the `f < n/3` Byzantine fault threshold, giving them the ability to propose blocks. They must create 1000 valid signed ingress messages (each ≤ 2 MB) targeting running canisters with sufficient cycles. The cycles check is cumulative per canister but the attacker can distribute messages across many canisters — all canister states are observable on-chain. The attacker must also serve the full 2 GB of message data to receiving replicas via the stripped-artifact fetch protocol, which they control as the block proposer. This is operationally feasible for a motivated Byzantine node operator. The attack is repeatable every consensus round.

## Recommendation

Add a memory-size check to `validate_ingress_payload` after the per-message loop, mirroring the check already present in `get_ingress_payload`:

```rust
// After the per-message loop, before returning:
let size_estimates = self.payload_size_estimates(payload);
let memory_limit = self
    .memory_byte_limit(NumBytes::new(u64::MAX), context.registry_version)
    .map_err(|e| ValidationError::ValidationFailed(
        IngressPayloadValidationFailure::RegistryError(e)
    ))?;
if size_estimates.memory > memory_limit {
    return Err(ValidationError::InvalidArtifact(
        InvalidIngressPayloadReason::PayloadTooLarge { ... }
    ));
}
Ok(size_estimates.wire)
```

Additionally, add a cumulative size guard in `try_insert` (or in the `assemble_message` loop) to abort fetching if the accumulated message size exceeds `max_ingress_bytes_per_block`, preventing OOM before validation runs.

## Proof of Concept

1. Byzantine replica (valid, below fault threshold) creates 1000 `SignedIngress` messages, each with a ~2 MB payload, distributed across many canisters with sufficient cycles.
2. It builds an `IngressPayload` from these messages and proposes a block containing it.
3. The block is stripped before broadcast: `ingress_payload = None` in the proto, with 1000 `StrippedIngressMessage` entries (~32 bytes each → ~32 KB total wire size).
4. Receiving replicas receive the stripped block, call `BlockProposalAssembler::new`, and spawn 1000 concurrent `get_or_fetch` tasks to retrieve the missing messages from the Byzantine proposer.
5. As messages are inserted via `try_insert`, the replica accumulates 1000 × 2 MB ≈ 2 GB of `SignedIngress` objects in memory.
6. `validate_ingress_payload` is called on the reconstructed payload: message count = 1000 ≤ 1000 ✓, each message ≤ 2 MB ✓, wire size = ~32 KB ≤ 8 MB ✓ — **validation passes**.
7. Replicas with insufficient memory headroom crash with OOM before or during validation. If a quorum of replicas crash, the subnet halts.

A deterministic integration test can be written using `PocketIC` or the replica test harness: construct an `IngressPayload` with 1000 messages each padded to `MAX_INGRESS_BYTES_PER_MESSAGE`, call `validate_ingress_payload` directly, and assert it returns `Ok(...)` — confirming the missing memory check. Then assert `size_estimates.memory > max_ingress_bytes_per_block` to confirm the amplification ratio.

### Citations

**File:** rs/consensus/features/src/lib.rs (L1-6)
```rust
/// [IC-1718]: Whether the `hashes-in-blocks` feature is enabled. If the flag is set to `true`, we
/// will strip all ingress messages and IDKG dealings from blocks, before sending them to peers.
/// On a receiver side, we will reconstruct the blocks by looking up the referenced ingress messages
/// in the ingress pool and IDKG dealings in the IDKG pool, or, if they are not there, by fetching
/// missing artifacts from peers who are advertising the blocks.
pub const HASHES_IN_BLOCKS_ENABLED: bool = true;
```

**File:** rs/ingress_manager/src/ingress_selector.rs (L213-220)
```rust
                    if accumulated_wire_size + size_estimates.wire > wire_byte_limit {
                        self.metrics.observe_limit_reached("wire_byte_limit");
                        break 'outer;
                    }
                    if accumulated_memory_size + size_estimates.memory > memory_byte_limit {
                        self.metrics.observe_limit_reached("memory_byte_limit");
                        break 'outer;
                    }
```

**File:** rs/ingress_manager/src/ingress_selector.rs (L291-293)
```rust
            if size_estimates.wire <= wire_byte_limit && size_estimates.memory <= memory_byte_limit
            {
                break payload;
```

**File:** rs/ingress_manager/src/ingress_selector.rs (L420-422)
```rust
        let size_estimates = self.payload_size_estimates(payload);

        Ok(size_estimates.wire)
```

**File:** rs/ingress_manager/src/ingress_selector.rs (L499-508)
```rust
        let ingress_message_size = signed_ingress.count_bytes();
        // The message is invalid if its size is larger than the configured maximum.
        if ingress_message_size > settings.max_ingress_bytes_per_message {
            return Err(ValidationError::InvalidArtifact(
                InvalidIngressPayloadReason::IngressMessageTooBig(
                    ingress_message_size,
                    settings.max_ingress_bytes_per_message,
                ),
            ));
        }
```

**File:** rs/ingress_manager/src/ingress_selector.rs (L644-658)
```rust
    fn payload_size_estimates(&self, payload: &IngressPayload) -> SizeEstimates {
        let memory_bytes =
            payload.total_messages_size_estimate() + payload.total_ids_size_estimate();

        let wire_bytes = if self.hashes_in_blocks_enabled() {
            payload.total_ids_size_estimate()
        } else {
            memory_bytes
        };

        SizeEstimates {
            memory: memory_bytes,
            wire: wire_bytes,
        }
    }
```

**File:** rs/consensus/src/consensus/payload_builder.rs (L144-168)
```rust
        let mut accumulated_size = NumBytes::new(0);
        for builder in &self.section_builder {
            accumulated_size +=
                builder.validate_payload(height, batch_payload, proposal_context, past_payloads)?;
        }

        // Check the combined size of the payloads using a 2x safety margin.
        // We allow payloads that are bigger than the maximum size but log an error.
        // And reject outright payloads that are more than twice the maximum size.
        if accumulated_size > max_block_payload_size {
            error!(
                self.logger,
                "The overall block size is too large, even though the individual payloads are valid: {}",
                CRITICAL_ERROR_PAYLOAD_TOO_LARGE
            );
            self.metrics.critical_error_payload_too_large.inc();
        }
        if accumulated_size > max_block_payload_size * 2 {
            return Err(ValidationError::InvalidArtifact(
                InvalidPayloadReason::PayloadTooBig {
                    expected: max_block_payload_size,
                    received: accumulated_size,
                },
            ));
        }
```

**File:** rs/limits/src/lib.rs (L71-78)
```rust
pub const MAX_BLOCK_PAYLOAD_SIZE: u64 = 4 * MEGABYTE;
/// How big an ingress payload can be *when stored in memory*. Increasing this value could lead to
/// increased memory usage of replicas.
/// Note that with hashes-in-blocks feature enabled, increasing this value doesn't necessarily mean
/// that we would send more data to peers when transmitting a block, because ingress messages are
/// stripped before disseminating blocks.
pub const MAX_INGRESS_BYTES_PER_BLOCK: u64 = 4 * MEGABYTE;
pub const MAX_INGRESS_MESSAGES_PER_BLOCK: u64 = 1000;
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L203-310)
```rust
        let mut join_set = tokio::task::JoinSet::new();

        let timer = self
            .metrics
            .download_missing_stripped_messages_duration
            .start_timer();
        let mut assembler = BlockProposalAssembler::new(stripped_block_proposal);

        let stripped_message_ids = assembler.missing_stripped_messages();
        // For each stripped object in the message, try to fetch it either from the local pools
        // or from a random peer who is advertising it.
        for stripped_message_id in stripped_message_ids {
            join_set.spawn(get_or_fetch(
                stripped_message_id,
                self.ingress_pool.clone(),
                self.idkg_pool.clone(),
                self.transport.clone(),
                id.as_ref().clone(),
                self.log.clone(),
                self.metrics.clone(),
                self.node_id,
                peer_rx.clone(),
            ));
        }

        let mut messages_from_pool = BTreeMap::<StrippedMessageType, usize>::new();
        let mut messages_from_peers = BTreeMap::<StrippedMessageType, usize>::new();

        // Abort the assembly as soon as the block proposal is no longer wanted. Returning
        // here drops `join_set`, which aborts all outstanding child fetch tasks.
        let mut bouncer = self.fetch_stripped.bouncer_watcher();

        loop {
            let join_result = tokio::select! {
                _ = bouncer.wait_for(|bouncer| matches!(bouncer(&id), BouncerValue::Unwanted)) => {
                    self.metrics.report_aborted_block_assembly();
                    return AssembleResult::Unwanted;
                }
                join_result = join_set.join_next() => join_result,
            };

            let Some(join_result) = join_result else {
                break;
            };

            let Ok((message, peer_id)) = join_result else {
                return AssembleResult::Unwanted;
            };

            let message_type = StrippedMessageType::from(&message);

            if peer_id == self.node_id {
                *messages_from_pool.entry(message_type).or_default() += 1;
            } else {
                self.metrics
                    .report_missing_stripped_messages_bytes(message_type, message.count_bytes());
                *messages_from_peers.entry(message_type).or_default() += 1;
            }

            if let Err(err) = assembler.try_insert_stripped_message(message) {
                warn!(
                    self.log,
                    "Failed to insert stripped message of type {}: {}. This is a bug.",
                    message_type.as_str(),
                    err
                );

                return AssembleResult::Unwanted;
            }
        }

        // Only report the metric if we actually downloaded some stripped messages from peers
        if !messages_from_peers.is_empty() {
            timer.stop_and_record();
        } else {
            timer.stop_and_discard();
        }

        for (message_type, count) in messages_from_peers.iter() {
            self.metrics.report_stripped_messages_count(
                StrippedMessageSource::Peer,
                *message_type,
                *count,
            );
        }
        for (message_type, count) in messages_from_pool.iter() {
            self.metrics.report_stripped_messages_count(
                StrippedMessageSource::Pool,
                *message_type,
                *count,
            );
        }

        match assembler.try_assemble() {
            Ok(reconstructed_block_proposal) => AssembleResult::Done {
                message: ConsensusMessage::BlockProposal(reconstructed_block_proposal),
                peer_id: peer,
            },
            Err(err) => {
                warn!(
                    self.log,
                    "Failed to reassemble the block {}. This is a bug.", err
                );

                AssembleResult::Unwanted
            }
        }
    }
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L435-454)
```rust
    fn try_insert(
        &mut self,
        signed_ingress_id: SignedIngressId,
        ingress_message: SignedIngress,
    ) -> Result<(), InsertionError> {
        // We can have at most 1000 elements in the vector, so it should be reasonably fast to do a
        // linear scan here.
        let (_, ingress) = self
            .ingress_messages
            .iter_mut()
            .find(|(id, _maybe_ingress)| *id == signed_ingress_id)
            .ok_or(InsertionError::NotNeeded)?;

        if ingress.is_some() {
            Err(InsertionError::AlreadyInserted)
        } else {
            *ingress = Some(ingress_message);
            Ok(())
        }
    }
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L456-473)
```rust
    fn try_reconstruct_payload(
        ingress_messages: Vec<(SignedIngressId, Option<SignedIngress>)>,
        payload: &mut Self::Payload,
    ) -> Result<(), AssemblyError> {
        let ingresses = ingress_messages
            .into_iter()
            .map(|(id, message)| {
                message
                    .ok_or_else(|| AssemblyError::MissingIngress(id.ingress_message_id.clone()))
                    .map(|message| (id.ingress_message_id, message))
            })
            .collect::<Result<Vec<_>, _>>()?;

        *payload = Some(pb::IngressPayload::from(IngressPayload::from_iter(
            ingresses,
        )));
        Ok(())
    }
```
