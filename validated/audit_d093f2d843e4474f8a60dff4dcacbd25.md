Audit Report

## Title
Unbounded Allocation in `build_transcripts_vec_from_pb` During DKG Payload Deserialization — (`rs/types/types/src/consensus/dkg.rs`)

## Summary
`build_transcripts_vec_from_pb` iterates over a caller-supplied `Vec<pb::CallbackIdedNiDkgTranscript>` with no count or size guard, allocating an unbounded `Vec<RemoteTranscriptResult>`. A Byzantine block proposer (a single node below the f < n/3 fault threshold) can craft a `pb::DkgDataPayload` or `pb::Summary` with arbitrarily many large entries, causing heap exhaustion on honest replicas before any validation logic runs. The constant `MAX_REMOTE_TRANSCRIPTS_PER_PAYLOAD = 2` is enforced only at payload creation time, never at deserialization time.

## Finding Description
`build_transcripts_vec_from_pb` at `rs/types/types/src/consensus/dkg.rs` lines 445–468 allocates a new `Vec` and pushes every entry from the caller-supplied slice with no capacity check:

```rust
fn build_transcripts_vec_from_pb(
    transcripts: Vec<pb::CallbackIdedNiDkgTranscript>,
) -> Result<Vec<RemoteTranscriptResult>, String> {
    let mut transcripts_for_remote_subnets = Vec::new();
    for transcript in transcripts.into_iter() {
        // ... decode each entry and push
        transcripts_for_remote_subnets.push(RemoteTranscriptResult { ... });
    }
    Ok(transcripts_for_remote_subnets)
}
```

This function is called unconditionally from both deserialization impls:
- `DkgDataPayload::TryFrom<pb::DkgDataPayload>` at lines 572–575
- `DkgSummary::TryFrom<pb::Summary>` at lines 525–528

`MAX_REMOTE_TRANSCRIPTS_PER_PAYLOAD` (value `2`) is defined in `rs/consensus/dkg/src/lib.rs` line 53 and imported only in `rs/consensus/dkg/src/payload_builder.rs` line 2 — it is applied only when building a payload, never when deserializing one.

The DKG payload validator (`validate_payload` in `rs/consensus/dkg/src/payload_validator.rs` line 30) accepts a `&BlockPayload`, meaning the payload is already fully deserialized before any validation logic executes. The `PayloadBuilderImpl::validate_payload` in `rs/consensus/src/consensus/payload_builder.rs` lines 127–171 enforces a block size limit only over `batch_payload` sections (Ingress, XNet, CanisterHttp, etc.); the DKG payload field is structurally separate and is not included in that size accounting. The artifact downloader at `rs/p2p/artifact_downloader/src/fetch_artifact/download.rs` line 52 explicitly disables HTTP body size limits with `.layer(DefaultBodyLimit::disable())`, so no transport-layer guard prevents an oversized block from reaching the deserialization path.

The `ErrorString` variant of `pb::NiDkgTranscriptResult` accepts an arbitrary `Vec<u8>` (validated only for UTF-8), so an attacker does not need to construct valid cryptographic transcript data. A payload with, e.g., 1,000 entries each carrying a 1 MB ASCII `ErrorString` causes ~1 GB of allocation inside `build_transcripts_vec_from_pb` before any error is returned.

## Impact Explanation
A Byzantine block proposer can crash honest replicas via heap exhaustion before any consensus validation rejects the crafted block. This constitutes an application/platform-level DoS and consensus availability impact not based on raw volumetric DDoS — matching the High severity impact class ($2,000–$10,000): *"Application/platform-level DoS, crash, consensus blocking, certified-state disruption, or subnet availability impact not based on raw volumetric DDoS."*

## Likelihood Explanation
A single Byzantine subnet node acting as block proposer for any round is within the IC consensus fault model (f < n/3). The attack requires no external resources, no key compromise, no coordination, and no victim mistakes. The crafted block is syntactically valid protobuf with a valid proposer signature. The attack is repeatable every round the Byzantine node holds block-proposer rank.

## Recommendation
Enforce a count bound on `transcripts` before iterating in `build_transcripts_vec_from_pb`:

```rust
fn build_transcripts_vec_from_pb(
    transcripts: Vec<pb::CallbackIdedNiDkgTranscript>,
) -> Result<Vec<RemoteTranscriptResult>, String> {
    const MAX_REMOTE_TRANSCRIPTS_PER_PAYLOAD: usize = 2;
    if transcripts.len() > MAX_REMOTE_TRANSCRIPTS_PER_PAYLOAD {
        return Err(format!(
            "Too many remote transcripts: {} > {}",
            transcripts.len(), MAX_REMOTE_TRANSCRIPTS_PER_PAYLOAD
        ));
    }
    // ... existing logic
}
```

The constant should be moved to (or re-exported from) the types crate so it is accessible at deserialization time without a circular dependency.

## Proof of Concept
Construct a `pb::DkgDataPayload` with many entries carrying large `ErrorString` values (no valid cryptographic data required):

```rust
let large_error = vec![b'A'; 1_000_000]; // 1 MB valid UTF-8
let transcripts: Vec<pb::CallbackIdedNiDkgTranscript> = (0..1_000)
    .map(|i| pb::CallbackIdedNiDkgTranscript {
        dkg_id: Some(/* valid NiDkgId proto */),
        transcript_result: Some(pb::NiDkgTranscriptResult {
            val: Some(pb::ni_dkg_transcript_result::Val::ErrorString(
                large_error.clone(), // 1 MB each
            )),
        }),
        callback_id: i,
    })
    .collect();

let payload = pb::DkgDataPayload {
    summary_height: 0,
    dealings: vec![],
    transcripts_for_remote_subnets: transcripts, // 1,000 × 1 MB = ~1 GB
};

// Allocates ~1 GB before returning — no size check occurs
let _ = DkgDataPayload::try_from(payload);
```

A unit test in `rs/types/types/src/consensus/dkg.rs` can confirm the missing guard: assert that `DkgDataPayload::try_from` with more than `MAX_REMOTE_TRANSCRIPTS_PER_PAYLOAD` entries returns an `Err` before the current fix, and `Ok` after.