Audit Report

## Title
Intentional 2× Safety Margin in `validate_payload` Allows Oversized Blocks Between 1× and 2× `max_block_payload_size` - (File: `rs/consensus/src/consensus/payload_builder.rs`)

## Summary
`PayloadBuilderImpl::validate_payload` applies a hard rejection only when `accumulated_size > max_block_payload_size * 2` (8 MiB for app subnets), while payloads between 4 MiB and 8 MiB are accepted with only a metric increment. A malicious subnet node acting as block proposer can craft a `BatchPayload` whose combined section sizes fall in this range, causing every honest validator to permanently accept, validate, and store an oversized block.

## Finding Description
In `rs/consensus/src/consensus/payload_builder.rs` lines 144–170, `validate_payload` accumulates sizes from each section validator and applies two checks:

- **Soft check** (line 153): `accumulated_size > max_block_payload_size` → logs a critical error metric, returns `Ok(())`.
- **Hard check** (line 161): `accumulated_size > max_block_payload_size * 2` → returns `Err(PayloadTooBig)`.

The comment at line 150 explicitly documents this as intentional: *"We allow payloads that are bigger than the maximum size but log an error. And reject outright payloads that are more than twice the maximum size."*

`MAX_BLOCK_PAYLOAD_SIZE = 4 MiB` (`rs/limits/src/lib.rs`, line 71). Individual section ceilings include `MAX_INGRESS_BYTES_PER_BLOCK = 4 MiB` (line 77) and `MAX_CANISTER_HTTP_PAYLOAD_SIZE = 2 MiB` (`rs/types/types/src/batch/canister_http.rs`, line 25). A proposer can fill ingress to 4 MiB and canister-HTTP to 2 MiB simultaneously; each section validator passes independently, and the combined 6 MiB satisfies `6 MiB < 8 MiB`, so the hard check is never triggered.

The test at lines 497–501 explicitly encodes this behavior: `#[case(6 * MB, true, false)]` — 6 MiB ingress + ~2 MiB from other sections triggers the soft error but **not** the hard rejection. The call path from `validator.rs` lines 1305–1320 confirms `validate_payload` is the gate before notarization.

## Impact Explanation
Fits **Medium ($200–$2,000)**: requires node/boundary-node control (subnet membership), but produces concrete, repeatable harm. Every honest replica must download (modulo hashes-in-blocks stripping for ingress), fully deserialize, validate, and permanently store each oversized finalized block. Repeated across many rounds this degrades disk, CPU, and I/O on all subnet nodes, and can increase per-round latency, raising the probability of higher-rank block proposals and unnecessary forks.

## Likelihood Explanation
Requires the attacker to be a subnet node below the fault threshold — a meaningful constraint (NNS approval needed). Once a node is a subnet member, it is selected as rank-0 proposer roughly once every `n` rounds with no additional collusion. No cryptographic material beyond the node's own consensus key is needed. The crafted payload requires only direct construction of a `BatchPayload` struct, bypassing `get_payload`.

## Recommendation
Replace the 2× hard limit with a strict rejection at `max_block_payload_size`:

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

If a small serialization-overhead margin is operationally necessary, use a fixed absolute value (e.g., a few KiB), not a full 2× multiplier. The existing test at lines 497–501 should be updated to expect `expects_hard_error: true` for any payload exceeding `max_block_payload_size`.

## Proof of Concept
The existing unit test at lines 497–569 of `payload_builder.rs` is a direct PoC: `#[case(6 * MB, true, false)]` passes a combined ~8 MiB payload through `validate_payload` and asserts `is_err() == false` (no hard rejection) while `count_critical_errors() > 0` (soft error fired). A malicious proposer replicates this by constructing a `BatchPayload` with ingress at 4 MiB and canister-HTTP at 2 MiB, wrapping it in `BlockPayload::Data`, signing with their consensus key, and broadcasting the `BlockProposal`. All validators call `validate_payload` via `check_block_validity` → `validator.rs:1305`, receive `Ok(())`, and proceed to notarize and finalize the block.