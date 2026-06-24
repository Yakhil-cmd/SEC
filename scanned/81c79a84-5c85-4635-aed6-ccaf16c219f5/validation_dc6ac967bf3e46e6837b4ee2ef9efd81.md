The code is clear enough to render a definitive verdict. Let me trace the exact path.

**`validate_ingress_payload` (lines 323–423):** [1](#0-0) 

It computes `size_estimates` but only returns `size_estimates.wire`. There is **no check of `size_estimates.memory` against any limit**.

**`payload_size_estimates` (lines 644–658):** [2](#0-1) 

When `hashes_in_blocks_enabled=true`: `wire_bytes = total_ids_size_estimate()` (IDs only), while `memory_bytes = total_messages_size_estimate() + total_ids_size_estimate()` (full messages + IDs). The validator returns only `wire_bytes`, never checking `memory_bytes`.

**The builder (`get_ingress_payload`) correctly enforces both limits:** [3](#0-2) 

But the validator does not mirror this check.

**Individual message checks in `validate_ingress`:** [4](#0-3) 

Only per-message size and count are checked — not the aggregate memory footprint.

**Default limits:** [5](#0-4) 

- `MAX_INGRESS_BYTES_PER_BLOCK = 4 MiB`
- `MAX_INGRESS_MESSAGES_PER_BLOCK = 1000`
- `MAX_INGRESS_BYTES_PER_MESSAGE_APP_SUBNET = 2 MiB`

Maximum memory a validated payload can carry: `1000 × 2 MiB = 2 GB` — **500× the intended memory limit**.

---

### Title
Missing aggregate memory-size check in `validate_ingress_payload` allows a Byzantine block proposer to force ~2 GB ingress payloads past consensus — (`rs/ingress_manager/src/ingress_selector.rs`)

### Summary

`validate_ingress_payload` enforces per-message size and message count but never checks the total in-memory size of the payload against `max_ingress_bytes_per_block`. A single Byzantine block proposer (within the BFT fault threshold) can craft a payload that passes all replica validation yet carries up to `max_ingress_messages_per_block × max_ingress_bytes_per_message` bytes of message data — 500× the intended limit on app subnets.

### Finding Description

`validate_ingress_payload` calls `payload_size_estimates` at line 420 and returns only `size_estimates.wire`:

```rust
let size_estimates = self.payload_size_estimates(payload);
Ok(size_estimates.wire)
```

`payload_size_estimates` computes both `memory_bytes` and `wire_bytes`, but the validator discards `memory_bytes` entirely. When `hashes_in_blocks_enabled=true`, `wire_bytes` equals only the sum of message IDs (~32 bytes each), so a payload with 1000 messages of 2 MiB each has `wire_bytes ≈ 32 KB` but `memory_bytes ≈ 2 GB`. The validator accepts it.

The builder (`get_ingress_payload`) correctly enforces both limits at lines 289–294, but the validator — which is what all honest replicas use to accept blocks from peers — does not. [6](#0-5) 

### Impact Explanation

Every honest replica on the subnet calls `validate_ingress_payload` before accepting a proposed block. A block carrying 2 GB of ingress message data passes validation, is finalized, and is handed to the execution environment. All replicas must deserialize and process the full message bodies, causing memory exhaustion and execution stalls across the entire subnet. This halts consensus and execution for the subnet until the block is processed or replicas crash.

### Likelihood Explanation

Requires a single Byzantine block proposer — one malicious or compromised replica node that is scheduled to propose a block. This is within the IC's Byzantine fault tolerance model (below the 1/3 threshold). No governance majority, no key compromise, no external dependency. The attacker only needs to be a subnet member and wait for their turn to propose.

### Recommendation

Add a memory-size check in `validate_ingress_payload` immediately after computing `size_estimates`, analogous to the check already present in `get_ingress_payload`:

```rust
let size_estimates = self.payload_size_estimates(payload);
let memory_limit = self.memory_byte_limit(/* wire_byte_limit */ ..., context.registry_version)?;
if size_estimates.memory > memory_limit {
    return Err(ValidationError::InvalidArtifact(
        InvalidIngressPayloadReason::PayloadTooBig { ... }
    ));
}
Ok(size_estimates.wire)
```

Note that `validate_ingress_payload` does not receive a `wire_byte_limit` parameter, so `memory_byte_limit` should be derived directly from `max_ingress_bytes_per_block` in the registry settings (as `memory_byte_limit` already does when `hashes_in_blocks_enabled=true`). [7](#0-6) 

### Proof of Concept

```rust
// Byzantine block proposer constructs payload directly (bypassing get_ingress_payload):
let messages: Vec<SignedIngress> = (0..1000)
    .map(|i| SignedIngressBuilder::new()
        .canister_id(canister_test_id(0))
        .nonce(i)
        .method_payload(vec![0u8; 2 * 1024 * 1024]) // 2 MiB each
        .expiry_time(valid_expiry)
        .build())
    .collect();

let payload = IngressPayload::from(messages); // ~2 GB total

// With hashes_in_blocks_enabled=true:
// wire_bytes  ≈ 1000 × 32 = 32 KB  → passes wire limit
// memory_bytes ≈ 1000 × 2 MiB = 2 GB → NOT checked, passes validation

let result = ingress_manager.validate_ingress_payload(
    &payload, &HashSet::new(), &validation_context
);
assert!(result.is_ok()); // passes — all replicas accept the block
// Execution environment now processes 2 GB of ingress data
```

### Citations

**File:** rs/ingress_manager/src/ingress_selector.rs (L289-294)
```rust
            let size_estimates = self.payload_size_estimates(&payload);

            if size_estimates.wire <= wire_byte_limit && size_estimates.memory <= memory_byte_limit
            {
                break payload;
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

**File:** rs/ingress_manager/src/ingress_selector.rs (L499-517)
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

        if num_messages >= settings.max_ingress_messages_per_block {
            return Err(ValidationError::InvalidArtifact(
                InvalidIngressPayloadReason::IngressPayloadTooManyMessages(
                    num_messages,
                    settings.max_ingress_messages_per_block,
                ),
            ));
        }
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

**File:** rs/limits/src/lib.rs (L77-86)
```rust
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
