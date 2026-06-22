### Title
Malicious Block Proposer Can Include Oversized `BatchPayload` That Passes Consensus Validation - (File: `rs/consensus/src/consensus/payload_builder.rs`)

### Summary
The `validate_payload` function in `PayloadBuilderImpl` enforces a hard rejection only when the combined payload size exceeds **twice** `max_block_payload_size` (i.e., 8 MiB for app subnets). Payloads between 1× and 2× the configured limit (4–8 MiB) are silently accepted with only a critical-error metric increment. A malicious block proposer can deliberately craft a `BatchPayload` in this range by filling multiple independent sections to their individual limits simultaneously, forcing every validator to download, fully validate, and permanently store an oversized block.

### Finding Description
`PayloadBuilderImpl::validate_payload` in `rs/consensus/src/consensus/payload_builder.rs` accumulates the byte-size returned by each section's own validator and then applies a two-tier check:

```rust
// lines 150-168
if accumulated_size > max_block_payload_size {
    error!(..., CRITICAL_ERROR_PAYLOAD_TOO_LARGE);
    self.metrics.critical_error_payload_too_large.inc();
}
if accumulated_size > max_block_payload_size * 2 {
    return Err(ValidationError::InvalidArtifact(
        InvalidPayloadReason::PayloadTooBig { ... },
    ));
}
``` [1](#0-0) 

The first branch is a **soft** guard — it logs and increments a metric but returns `Ok(())`. The second branch is the only hard rejection, set at `2 × max_block_payload_size`. The configured maximum is `MAX_BLOCK_PAYLOAD_SIZE = 4 MiB`: [2](#0-1) 

Each section validator enforces its own independent ceiling:
- Ingress: up to `MAX_INGRESS_BYTES_PER_BLOCK = 4 MiB`
- CanisterHttp: up to `MAX_CANISTER_HTTP_PAYLOAD_SIZE = 2 MiB`
- QueryStats, ChainKey, XNet, Bitcoin: additional MBs each [3](#0-2) 

The test at lines 497–569 explicitly documents that a 6 MiB ingress payload combined with ~2 MiB from other sections (totalling ~8 MiB) triggers only the soft error, not a hard rejection: [4](#0-3) 

A malicious proposer bypasses `get_payload` entirely and constructs a `BatchPayload` struct directly, filling ingress to 4 MiB and canister-HTTP to 2 MiB simultaneously. The combined 6 MiB payload passes every section validator individually and passes the top-level check because `6 MiB < 2 × 4 MiB`.

`validate_payload` is called inside `check_block_validity` before any notarization signature is cast: [5](#0-4) 

Because notarization requires this check to succeed, every honest validator on the subnet must fully validate and then store the oversized block.

### Impact Explanation
**Medium.** Every honest replica must:
1. Download the oversized block over P2P (up to 2× the normal maximum).
2. Run all section validators against it (deserializing ingress messages, canister-HTTP responses, query-stats entries, chain-key data).
3. Permanently store the finalized block on disk.

Repeated across many rounds, this shrinks available disk space and increases per-round CPU and I/O cost on every node. On subnets with tighter hardware margins this can delay notarization, increasing the risk of a competing higher-rank block being proposed and causing unnecessary forks or slashing exposure.

### Likelihood Explanation
**Medium.** Block proposers are selected in rank order from the subnet membership. Any single malicious node below the fault threshold will be selected as rank-0 proposer roughly once per `n` rounds (where `n` is subnet size). No collusion is required; a single compromised or adversarial node suffices. The crafted payload requires no special cryptographic material — only the ability to sign a block proposal with the node's consensus key.

### Recommendation
Replace the 2× safety margin with a strict hard limit at `max_block_payload_size`. The comment acknowledges the margin exists because "individual payloads are valid" even when their sum exceeds the block limit; the correct fix is to enforce the combined limit strictly:

```rust
if accumulated_size > max_block_payload_size {
    return Err(ValidationError::InvalidArtifact(
        InvalidPayloadReason::PayloadTooBig {
            expected: max_block_payload_size,
            received: accumulated_size,
        },
    ));
}
```

If a grace margin is needed for operational reasons, it should be a small absolute value (e.g., a few KB for serialization overhead), not a full 2× multiplier that allows an attacker to double the effective block size.

### Proof of Concept
A malicious node acting as block proposer constructs a `BatchPayload` directly (bypassing `get_payload`):

```rust
let oversized_payload = BatchPayload {
    ingress: /* 4 MiB of valid ingress messages, ≤ MAX_INGRESS_BYTES_PER_BLOCK */,
    canister_http: /* 2 MiB of valid canister-HTTP bytes, ≤ MAX_CANISTER_HTTP_PAYLOAD_SIZE */,
    xnet: XNetPayload::default(),
    self_validating: SelfValidatingPayload::default(),
    query_stats: vec![0u8; 512 * 1024],  // 512 KiB raw bytes
    chain_key: vec![0u8; 512 * 1024],    // 512 KiB raw bytes
};
// Combined ≈ 7.5 MiB — above max_block_payload_size (4 MiB)
//                     — below 2 × max_block_payload_size (8 MiB)
```

The proposer wraps this in a `BlockPayload::Data`, signs it with its consensus key, and broadcasts the `BlockProposal`. Every validator calls `validate_payload`, which:
1. Passes each section validator (each section is within its own limit).
2. Reaches the combined-size check: `7.5 MiB > 4 MiB` → logs a critical error metric.
3. Reaches the hard-rejection check: `7.5 MiB < 8 MiB` → **does not reject**.
4. Returns `Ok(())`.

The block is notarized, finalized, and stored by all replicas. [6](#0-5)

### Citations

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

**File:** rs/consensus/src/consensus/payload_builder.rs (L497-502)
```rust
    #[rstest]
    #[case(2 * MB, false, false)]
    #[case(3 * MB, true, false)]
    #[case(6 * MB, true, false)]
    #[case(7 * MB, true, true)]
    // Note: payloads other than the ingress payload sum to a little below 2 MB.
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

**File:** rs/types/types/src/batch/canister_http.rs (L25-25)
```rust
pub const MAX_CANISTER_HTTP_PAYLOAD_SIZE: usize = 2 * 1024 * 1024; // 2 MiB
```

**File:** rs/consensus/src/consensus/validator.rs (L1305-1320)
```rust
        self.payload_builder
            .validate_payload(
                proposal.height,
                &ProposalContext {
                    proposer,
                    validation_context: &proposal.context,
                },
                &proposal.payload,
                &past_payloads,
            )
            .map_err(|err| {
                err.map(
                    InvalidArtifactReason::InvalidPayload,
                    ValidationFailure::PayloadValidationFailed,
                )
            })?;
```
