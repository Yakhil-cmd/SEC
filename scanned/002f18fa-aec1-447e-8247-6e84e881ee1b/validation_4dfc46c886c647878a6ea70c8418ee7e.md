I need to trace the exact validation path to determine if the memory-size invariant is actually enforced during `validate_ingress_payload` when `hashes_in_blocks_enabled = true`.

### Title
Missing `max_ingress_bytes_per_block` Memory-Size Check in `validate_ingress_payload` When `hashes_in_blocks` Is Enabled — (`rs/ingress_manager/src/ingress_selector.rs`)

---

### Summary

`validate_ingress_payload` never checks the total reconstructed memory size of an `IngressPayload` against `max_ingress_bytes_per_block`. When `hashes_in_blocks_enabled = true`, the wire size of a block is only the sum of message IDs (~32 bytes each), so a payload containing 1000 × 2 MB messages passes every wire-size guard with a 32 KB footprint, while its in-memory reconstruction is ~2 GB — 500× the intended 4 MB limit.

---

### Finding Description

**Asymmetry between `get_ingress_payload` and `validate_ingress_payload`**

`get_ingress_payload` correctly enforces both limits:

```
// rs/ingress_manager/src/ingress_selector.rs  lines 213-220
if accumulated_wire_size + size_estimates.wire > wire_byte_limit { break 'outer; }
if accumulated_memory_size + size_estimates.memory > memory_byte_limit { break 'outer; }
``` [1](#0-0) 

`memory_byte_limit` is derived from `max_ingress_bytes_per_block` (4 MB) when `hashes_in_blocks_enabled`:

```rust
// lines 634-635
if self.hashes_in_blocks_enabled() {
    Ok(NumBytes::new(memory_byte_limit))   // = max_ingress_bytes_per_block
``` [2](#0-1) 

`validate_ingress_payload`, however, only checks:
1. `payload.message_count() > settings.max_ingress_messages_per_block` (line 373)
2. Per-message size ≤ `settings.max_ingress_bytes_per_message` (line 501)
3. Returns `size_estimates.wire` — the ID-only size — without ever comparing `size_estimates.memory` to `max_ingress_bytes_per_block`:

```rust
// lines 420-422
let size_estimates = self.payload_size_estimates(payload);
Ok(size_estimates.wire)   // memory never checked
``` [3](#0-2) 

`payload_size_estimates` computes `memory_bytes` but it is silently discarded:

```rust
// lines 644-657
fn payload_size_estimates(&self, payload: &IngressPayload) -> SizeEstimates {
    let memory_bytes = payload.total_messages_size_estimate() + payload.total_ids_size_estimate();
    let wire_bytes = if self.hashes_in_blocks_enabled() {
        payload.total_ids_size_estimate()   // IDs only
    } else { memory_bytes };
    SizeEstimates { memory: memory_bytes, wire: wire_bytes }
}
``` [4](#0-3) 

**Upstream `validate_payload` also only accumulates wire sizes**

`PayloadBuilderImpl::validate_payload` sums the values returned by each section's `validate_payload` and compares against `max_block_payload_size * 2`. For ingress with `hashes_in_blocks`, the returned value is the wire (ID-only) size, so 1000 messages contribute only ~32 KB to the accumulated total, far below the 8 MB hard-reject threshold: [5](#0-4) 

**Limits in production**

```
MAX_INGRESS_BYTES_PER_BLOCK          = 4 MB   (intended memory cap)
MAX_INGRESS_MESSAGES_PER_BLOCK       = 1000
MAX_INGRESS_BYTES_PER_MESSAGE_APP    = 2 MB   (per-message hard limit)
HASHES_IN_BLOCKS_ENABLED             = true   (currently live)
``` [6](#0-5) [7](#0-6) 

Worst-case: 1000 × 2 MB = **2 GB** reconstructed payload, validated and accepted.

---

### Impact Explanation

When a receiving replica assembles a `StrippedBlockProposal`, it fetches each referenced ingress message from the pool or peers and inserts them into `BlockProposalAssembler` before calling `validate_ingress_payload`: [8](#0-7) 

All 1000 × 2 MB messages are held in memory simultaneously during assembly. `validate_ingress_payload` then passes (no memory-total check), the block is accepted, and the 2 GB allocation persists through execution. On replicas with constrained memory or under concurrent load this causes OOM or severe memory pressure, stalling consensus on that node. If enough honest replicas are affected, the subnet halts.

---

### Likelihood Explanation

- **Attacker role**: A single Byzantine replica below the fault threshold — explicitly within scope.
- **Precondition**: The attacker must have 1000 × 2 MB valid, signed, non-expired ingress messages in the pool of honest replicas. The default `ingress_pool_max_count = usize::MAX` and `ingress_pool_max_bytes = usize::MAX` mean pool admission is effectively unbounded; throttling is a soft 503 to users, not a hard cap. [9](#0-8) 

- **Cost**: Submitting 2 GB of ingress data is expensive in cycles and bandwidth, but a well-resourced attacker can do it. Each message passes the per-message size check at the HTTP endpoint and at `validate_ingress_pool_object`. [10](#0-9) 

- **Realistic bound**: The maximum memory amplification is bounded at 2 GB (not unbounded), so OOM is not guaranteed on all hardware. However, it is a concrete, reproducible violation of the `max_ingress_bytes_per_block` invariant that the codebase explicitly documents as a memory-usage guard.

---

### Recommendation

Add a total-memory check inside `validate_ingress_payload`, after computing `size_estimates`, using the same `max_ingress_bytes_per_block` value already available from `settings`:

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

---

### Proof of Concept

```
1. Byzantine replica submits 1000 SignedIngress messages, each with a
   ~2 MB method_payload, to the subnet. Each passes the HTTP endpoint
   size check (≤ MAX_INGRESS_BYTES_PER_MESSAGE_APP_SUBNET).

2. Messages propagate to honest replicas' ingress pools.

3. Byzantine block maker calls get_ingress_payload with a large
   wire_byte_limit, bypassing the honest memory_byte_limit guard
   (or directly constructs an IngressPayload with all 1000 messages).

4. Block is stripped before broadcast:
     wire_size = 1000 × EXPECTED_MESSAGE_ID_LENGTH ≈ 32 KB

5. Receiving replicas receive StrippedBlockProposal, fetch all 1000
   full messages (2 GB total), assemble the block.

6. validate_ingress_payload is called:
     - message_count = 1000 ≤ 1000  ✓
     - each message ≤ 2 MB           ✓
     - wire size = 32 KB ≤ 4 MB      ✓
     - memory = 2 GB — NOT CHECKED   ✓ (passes)

7. Block is accepted. 2 GB remains allocated. Replica OOMs or
   experiences severe memory pressure, stalling consensus.

State-machine test assertion:
  assert!(validate_ingress_payload(payload_with_1000_x_2mb).is_ok());
  // passes today — should fail with IngressPayloadTooLarge
```

### Citations

**File:** rs/ingress_manager/src/ingress_selector.rs (L212-220)
```rust
                    // Break criterion #1: global byte limit
                    if accumulated_wire_size + size_estimates.wire > wire_byte_limit {
                        self.metrics.observe_limit_reached("wire_byte_limit");
                        break 'outer;
                    }
                    if accumulated_memory_size + size_estimates.memory > memory_byte_limit {
                        self.metrics.observe_limit_reached("memory_byte_limit");
                        break 'outer;
                    }
```

**File:** rs/ingress_manager/src/ingress_selector.rs (L420-422)
```rust
        let size_estimates = self.payload_size_estimates(payload);

        Ok(size_estimates.wire)
```

**File:** rs/ingress_manager/src/ingress_selector.rs (L619-642)
```rust
    fn memory_byte_limit(
        &self,
        wire_byte_limit: NumBytes,
        registry_version: RegistryVersion,
    ) -> Result<NumBytes, String> {
        let memory_byte_limit = self
            .get_ingress_message_settings(registry_version)
            .map_err(|err| {
                format!(
                    "Failed to get ingress message settings \
                    at registry version {registry_version}: {err}"
                )
            })?
            .max_ingress_bytes_per_block as u64;

        if self.hashes_in_blocks_enabled() {
            Ok(NumBytes::new(memory_byte_limit))
        } else {
            Ok(NumBytes::new(std::cmp::min(
                wire_byte_limit.get(),
                memory_byte_limit,
            )))
        }
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

**File:** rs/limits/src/lib.rs (L71-86)
```rust
pub const MAX_BLOCK_PAYLOAD_SIZE: u64 = 4 * MEGABYTE;
/// How big an ingress payload can be *when stored in memory*. Increasing this value could lead to
/// increased memory usage of replicas.
/// Note that with hashes-in-blocks feature enabled, increasing this value doesn't necessarily mean
/// that we would send more data to peers when transmitting a block, because ingress messages are
/// stripped before disseminating blocks.
pub const MAX_INGRESS_BYTES_PER_BLOCK: u64 = 4 * MEGABYTE;
pub const MAX_INGRESS_MESSAGES_PER_BLOCK: u64 = 1000;
/// This sets the upper bound on how big a single ingress message can be, as
/// allowing messages larger than around 3.5MB has various security and
/// performance impacts on the network.  More specifically, large messages can
/// allow dishonest block makers to always manage to get their blocks notarized;
/// and when the consensus protocol is configured for smaller messages, a large
/// message in the network can cause the finalization rate to drop.
pub const MAX_INGRESS_BYTES_PER_MESSAGE_APP_SUBNET: u64 = 2 * MEGABYTE;
pub const MAX_INGRESS_BYTES_PER_MESSAGE_NNS_SUBNET: u64 = 3 * MEGABYTE + 512 * KILOBYTE;
```

**File:** rs/consensus/features/src/lib.rs (L1-6)
```rust
/// [IC-1718]: Whether the `hashes-in-blocks` feature is enabled. If the flag is set to `true`, we
/// will strip all ingress messages and IDKG dealings from blocks, before sending them to peers.
/// On a receiver side, we will reconstruct the blocks by looking up the referenced ingress messages
/// in the ingress pool and IDKG dealings in the IDKG pool, or, if they are not there, by fetching
/// missing artifacts from peers who are advertising the blocks.
pub const HASHES_IN_BLOCKS_ENABLED: bool = true;
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L571-649)
```rust
impl BlockProposalAssembler {
    fn new(stripped_block_proposal: StrippedBlockProposal) -> Self {
        Self {
            ingress_messages: stripped_block_proposal
                .stripped_ingress_payload
                .ingress_messages
                .iter()
                .map(|signed_ingress_id| (signed_ingress_id.clone(), None))
                .collect(),
            signed_dealings: stripped_block_proposal
                .stripped_idkg_dealings
                .stripped_dealings
                .iter()
                .map(|(node_index, dealing_id)| ((*node_index, dealing_id.clone()), None))
                .collect(),
            stripped_block_proposal,
        }
    }

    /// Returns the list of messages which have been stripped from the block.
    pub(crate) fn missing_stripped_messages(&self) -> Vec<StrippedMessageId> {
        let ingress_messages = PayloadAssembler::<SignedIngress>::missing_artifacts(self)
            .map(StrippedMessageId::Ingress);
        let idkg_dealings = PayloadAssembler::<SignedIDkgDealing>::missing_artifacts(self)
            .map(|(node_index, dealing_id)| StrippedMessageId::IDkgDealing(dealing_id, node_index));

        ingress_messages.chain(idkg_dealings).collect()
    }

    /// Tries to insert a missing stripped message into the block.
    pub(crate) fn try_insert_stripped_message(
        &mut self,
        message: StrippedMessage,
    ) -> Result<(), InsertionError> {
        match message {
            StrippedMessage::Ingress(signed_ingress_id, signed_ingress) => {
                self.try_insert(signed_ingress_id, signed_ingress)
            }
            StrippedMessage::IDkgDealing(dealing_id, _, signed_dealing) => {
                self.try_insert(dealing_id, signed_dealing)
            }
        }
    }

    /// Tries to reassemble a block.
    ///
    /// Fails if there are still some stripped messages missing,
    /// the assembled proposal can't be deserialized, or if the
    /// assembled proposal has an ID that doesn't match the claimed ID.
    pub(crate) fn try_assemble(self) -> Result<BlockProposal, AssemblyError> {
        let BlockProposalAssembler {
            stripped_block_proposal,
            ingress_messages,
            signed_dealings,
        } = self;
        let claimed_id = stripped_block_proposal.unstripped_consensus_message_id;
        let mut reconstructed_block_proposal_proto =
            stripped_block_proposal.pruned_block_proposal_proto;

        if let Some(block) = reconstructed_block_proposal_proto.value.as_mut() {
            Self::try_reconstruct_payload(ingress_messages, &mut block.ingress_payload)?;
            if let Some(idkg) = block.idkg_payload.as_mut() {
                Self::try_reconstruct_payload(signed_dealings, idkg)?;
            }
        }

        let reconstructed_block_proposal: BlockProposal = reconstructed_block_proposal_proto
            .try_into()
            .map_err(AssemblyError::DeserializationFailed)?;
        let assembled_id = reconstructed_block_proposal.get_id();
        if assembled_id != claimed_id {
            return Err(AssemblyError::MismatchedConsensusMessageId {
                claimed: claimed_id,
                assembled: assembled_id,
            });
        }

        Ok(reconstructed_block_proposal)
    }
```

**File:** rs/config/src/artifact_pool.rs (L33-44)
```rust
impl ArtifactPoolTomlConfig {
    /// Create a ArtifactPoolTomlConfig from a given path to the consensus pool.
    pub fn new(consensus_pool_path: PathBuf, backup: Option<BackupConfig>) -> Self {
        Self {
            consensus_pool_path,
            ingress_pool_max_count: usize::MAX,
            ingress_pool_max_bytes: usize::MAX,
            consensus_pool_backend: Some("lmdb".to_string()),
            backup,
        }
    }
}
```

**File:** rs/ingress_manager/src/ingress_handler.rs (L175-182)
```rust
        // If the message is too large, consider the ingress message invalid
        let size = ingress_object.count_bytes();
        if size > settings.max_ingress_bytes_per_message {
            return Err(IngressMessageValidationError::IngressMessageTooLarge {
                max: settings.max_ingress_bytes_per_message,
                actual: size,
            });
        }
```
