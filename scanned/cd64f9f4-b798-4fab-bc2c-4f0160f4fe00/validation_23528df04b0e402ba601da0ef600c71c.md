Based on my thorough code analysis, here is the assessment:

---

### Title
Missing Memory-Size Bound in `validate_ingress_payload` Enables OOM via `hashes_in_blocks` Amplification — (`rs/ingress_manager/src/ingress_selector.rs`)

### Summary

When `hashes_in_blocks` is enabled (it is hardcoded `true` in production), `validate_ingress_payload` enforces only the wire-size (hash IDs) of an ingress payload, not its in-memory size. A Byzantine block maker below the fault threshold can craft a block containing up to `max_ingress_messages_per_block × max_ingress_bytes_per_message` (1000 × 2 MB = 2 GB) of ingress data. The stripped block passes all wire-size checks (~32 KB), but receiving replicas allocate 2 GB of memory during block reconstruction — before validation even runs — causing OOM and consensus stall.

### Finding Description

**`hashes_in_blocks` is enabled in production:** [1](#0-0) 

When enabled, block proposals are stripped of their ingress payload before broadcast. Receiving replicas reconstruct the block by fetching full messages from the ingress pool or from peers: [2](#0-1) 

The assembler fetches all missing messages and reconstructs the full `IngressPayload` with no size guard: [3](#0-2) 

**`validate_ingress_payload` does not check total memory size:**

The function checks message count and per-message validity, then computes size estimates — but only returns `size_estimates.wire` (hash IDs only when `hashes_in_blocks` is enabled), with no check of `size_estimates.memory` against `max_ingress_bytes_per_block`: [4](#0-3) 

`payload_size_estimates` correctly computes both `memory` and `wire` estimates: [5](#0-4) 

But `validate_ingress_payload` discards the `memory` field entirely and returns only `wire`. The consensus-level `validate_payload` then checks the accumulated wire size against `max_block_payload_size`: [6](#0-5) 

With 1000 × 32-byte hashes = 32 KB wire size, this check trivially passes.

**Contrast with `get_ingress_payload` (honest path):** The honest block builder enforces *both* limits: [7](#0-6) 

And `memory_byte_limit` is set to `max_ingress_bytes_per_block` (4 MB) when `hashes_in_blocks` is enabled: [8](#0-7) 

The validation path has no equivalent enforcement.

**Per-message size is bounded but total is not:**

`validate_ingress` checks each individual message against `max_ingress_bytes_per_message` (2 MB): [9](#0-8) 

So 1000 messages × 2 MB each = 2 GB all individually pass. The aggregate memory bound (`max_ingress_bytes_per_block = 4 MB`) is never enforced during validation. [10](#0-9) 

### Impact Explanation

The OOM occurs during block **reconstruction** (fetching 1000 × 2 MB messages), which happens *before* `validate_ingress_payload` is called. Even if validation were to reject the block afterward, the memory has already been allocated. On a replica with typical memory headroom, allocating 2 GB for a single block proposal causes an OOM crash. If enough honest replicas crash, the subnet loses its ability to finalize blocks and halts.

### Likelihood Explanation

The Byzantine block maker must be a valid replica (below the f < n/3 fault threshold). It needs to create 1000 valid signed ingress messages (each ≤ 2 MB), targeting running canisters with sufficient cycles. The cycles check is cumulative per canister, but the attacker can distribute messages across many canisters. The attacker must also serve the full messages to receiving replicas (via the stripped-artifact fetch protocol), which it controls as the block proposer. This is operationally feasible for a motivated Byzantine replica.

### Recommendation

Add a memory-size check to `validate_ingress_payload`, mirroring the check already present in `get_ingress_payload`:

```rust
// After the per-message loop, before returning:
let size_estimates = self.payload_size_estimates(payload);
let memory_limit = self.memory_byte_limit(/* wire_byte_limit */ NumBytes::new(u64::MAX), context.registry_version)?;
if size_estimates.memory > memory_limit {
    return Err(ValidationError::InvalidArtifact(
        InvalidIngressPayloadReason::PayloadTooLarge { ... }
    ));
}
Ok(size_estimates.wire)
```

Additionally, add a size guard in the block reconstruction assembler (`try_reconstruct_payload`) to abort fetching if the accumulated message size exceeds `max_ingress_bytes_per_block`, preventing OOM before validation runs.

### Proof of Concept

1. Byzantine replica (valid, below fault threshold) creates 1000 `SignedIngress` messages, each with a 2 MB payload, targeting a canister with sufficient cycles.
2. It builds an `IngressPayload` from these messages and proposes a block containing it.
3. The block is stripped before broadcast: `ingress_payload = None` in the proto, with 1000 `StrippedIngressMessage` entries (32 bytes each → 32 KB total).
4. Receiving replicas receive the stripped block, call `BlockProposalAssembler::new`, and begin fetching the 1000 missing messages from the Byzantine proposer.
5. As messages are inserted via `try_insert`, the replica allocates 1000 × 2 MB = 2 GB of memory.
6. `validate_ingress_payload` is called on the reconstructed payload: message count = 1000 ≤ 1000 ✓, each message ≤ 2 MB ✓, wire size = 32 KB ≤ 4 MB ✓ — **validation passes**.
7. The replica crashes with OOM before or during validation. If a quorum of replicas crash, the subnet halts.

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

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/stripper.rs (L28-73)
```rust
impl Strippable for ConsensusMessage {
    type Output = MaybeStrippedConsensusMessage;

    fn strip(self) -> Self::Output {
        let unstripped_consensus_message_id = self.id();

        match self {
            // We only strip data blocks.
            ConsensusMessage::BlockProposal(block_proposal)
                if block_proposal.as_ref().payload.payload_type()
                    == ic_types::consensus::PayloadType::Data =>
            {
                let DataPayload {
                    batch: BatchPayload { ingress, .. },
                    idkg,
                    ..
                } = block_proposal.content.as_ref().payload.as_ref().as_data();

                let stripped_ingress_payload = ingress.strip();
                let stripped_idkg_dealings = idkg.strip();

                let mut proto = pb::BlockProposal::from(block_proposal);

                if let Some(block) = proto.value.as_mut() {
                    // Remove the ingress payload from the proto.
                    block.ingress_payload = None;
                    // Remove the IDKG dealings from the proto.
                    if let Some(idkg) = block.idkg_payload.as_mut() {
                        for transcript in &mut idkg.idkg_transcripts {
                            for dealing in &mut transcript.verified_dealings {
                                dealing.signed_dealing_tuple = None;
                            }
                        }
                    }
                }

                MaybeStrippedConsensusMessage::StrippedBlockProposal(StrippedBlockProposal {
                    pruned_block_proposal_proto: proto,
                    unstripped_consensus_message_id,
                    stripped_ingress_payload,
                    stripped_idkg_dealings,
                })
            }
            msg => MaybeStrippedConsensusMessage::Unstripped(msg),
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

**File:** rs/ingress_manager/src/ingress_selector.rs (L373-422)
```rust
        if payload.message_count() > settings.max_ingress_messages_per_block {
            return Err(ValidationError::InvalidArtifact(
                InvalidIngressPayloadReason::IngressPayloadTooManyMessages(
                    payload.message_count(),
                    settings.max_ingress_messages_per_block,
                ),
            ));
        }

        // Tracks the sum of cycles needed per canister.
        let mut cycles_needed: BTreeMap<CanisterId, Cycles> = BTreeMap::new();

        // Validate each ingress message in the payload
        for (ingress_id, maybe_ingress) in payload.iter() {
            let ingress = match maybe_ingress {
                Ok(ingress) => ingress,
                Err(deserialization_error) => {
                    return Err(ValidationError::InvalidArtifact(
                        InvalidIngressPayloadReason::IngressMessageDeserializationFailure(
                            ingress_id.clone(),
                            deserialization_error.to_string(),
                        ),
                    ));
                }
            };

            if IngressMessageId::from(&ingress) != *ingress_id {
                return Err(ValidationError::InvalidArtifact(
                    InvalidIngressPayloadReason::MismatchedMessageId {
                        expected: ingress_id.clone(),
                        computed: IngressMessageId::from(&ingress),
                    },
                ));
            }

            self.validate_ingress(
                ingress_id.clone(),
                &ingress,
                &state,
                context,
                &settings,
                &past_ingress,
                0, // message count is checked above.
                &mut cycles_needed,
            )?;
        }

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

**File:** rs/ingress_manager/src/ingress_selector.rs (L634-641)
```rust
        if self.hashes_in_blocks_enabled() {
            Ok(NumBytes::new(memory_byte_limit))
        } else {
            Ok(NumBytes::new(std::cmp::min(
                wire_byte_limit.get(),
                memory_byte_limit,
            )))
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
