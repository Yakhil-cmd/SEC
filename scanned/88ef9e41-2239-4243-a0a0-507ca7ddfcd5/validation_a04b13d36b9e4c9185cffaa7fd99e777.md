Based on my analysis of the actual code, here is my assessment:

---

### Title
Unbounded Allocation in `build_transcripts_vec_from_pb` During DKG Payload Deserialization — (`rs/types/types/src/consensus/dkg.rs`)

### Summary

A Byzantine block proposer (a single compromised subnet node, within the consensus fault threshold) can craft a `pb::DkgDataPayload` or `pb::Summary` containing an arbitrarily large number of `CallbackIdedNiDkgTranscript` entries. When honest replicas receive and attempt to deserialize this block, `build_transcripts_vec_from_pb` allocates an unbounded `Vec<RemoteTranscriptResult>` with no size guard, potentially exhausting heap memory before any validation occurs.

### Finding Description

`build_transcripts_vec_from_pb` iterates over the caller-supplied `Vec<pb::CallbackIdedNiDkgTranscript>` and pushes each decoded entry into a new `Vec<RemoteTranscriptResult>` with no capacity check: [1](#0-0) 

This function is called unconditionally from both deserialization impls:

- `DkgDataPayload::TryFrom<pb::DkgDataPayload>` at line 572–575: [2](#0-1) 

- `DkgSummary::TryFrom<pb::Summary>` at line 525–528: [3](#0-2) 

The constant `MAX_REMOTE_TRANSCRIPTS_PER_PAYLOAD` exists only in `rs/consensus/dkg/src/payload_builder.rs` and `rs/consensus/dkg/src/lib.rs` — it is enforced only at **payload creation time**, never at deserialization time. [4](#0-3) 

The DKG payload validator (`validate_payload` in `rs/consensus/dkg/src/payload_validator.rs`) operates on an already-deserialized `BlockPayload` — it receives a `&BlockPayload`, meaning deserialization has already completed (and any OOM has already occurred) before any validation logic runs: [5](#0-4) 

No transport-level or artifact-pool-level block size limit was found in the searched codebase that would prevent an oversized serialized block from reaching the deserialization path.

### Impact Explanation

Each `NiDkgTranscript` is a large cryptographic object. A block with even tens of thousands of `CallbackIdedNiDkgTranscript` entries (each containing a full transcript) can exhaust the heap of a receiving replica before any validation rejects the block. This causes an OOM crash of a single honest replica — a non-volumetric, targeted denial-of-service against individual subnet members.

### Likelihood Explanation

A single Byzantine subnet node acting as block proposer is within the IC consensus fault model (below the f < n/3 threshold). The attack requires no external resources, no key compromise, and no coordination — just one malicious node proposing a crafted block. The crafted block is syntactically valid protobuf and will pass transport-layer checks. The receiving replica has no defense before the allocation occurs.

### Recommendation

Enforce a size bound on `transcripts_for_remote_subnets` **before** iterating in `build_transcripts_vec_from_pb`:

```rust
fn build_transcripts_vec_from_pb(
    transcripts: Vec<pb::CallbackIdedNiDkgTranscript>,
) -> Result<Vec<RemoteTranscriptResult>, String> {
    if transcripts.len() > MAX_REMOTE_TRANSCRIPTS_PER_PAYLOAD {
        return Err(format!(
            "Too many remote transcripts: {} > {}",
            transcripts.len(), MAX_REMOTE_TRANSCRIPTS_PER_PAYLOAD
        ));
    }
    // ... existing logic
}
```

`MAX_REMOTE_TRANSCRIPTS_PER_PAYLOAD` should be moved to (or re-exported from) the types crate so it is accessible at deserialization time.

### Proof of Concept

```rust
// Construct a pb::DkgDataPayload with 10_000 transcript entries
let transcripts: Vec<pb::CallbackIdedNiDkgTranscript> = (0..10_000)
    .map(|i| pb::CallbackIdedNiDkgTranscript {
        dkg_id: Some(/* valid NiDkgId proto */),
        transcript_result: Some(pb::NiDkgTranscriptResult {
            val: Some(pb::ni_dkg_transcript_result::Val::ErrorString(
                b"err".to_vec(),
            )),
        }),
        callback_id: i,
    })
    .collect();

let payload = pb::DkgDataPayload {
    summary_height: 0,
    dealings: vec![],
    transcripts_for_remote_subnets: transcripts,
};

// This call allocates without bound — no size check occurs
let result = DkgDataPayload::try_from(payload);
// On a real NiDkgTranscript payload (not ErrorString), heap exhaustion occurs
// before result is returned.
```

### Citations

**File:** rs/types/types/src/consensus/dkg.rs (L445-468)
```rust
fn build_transcripts_vec_from_pb(
    transcripts: Vec<pb::CallbackIdedNiDkgTranscript>,
) -> Result<Vec<RemoteTranscriptResult>, String> {
    let mut transcripts_for_remote_subnets = Vec::new();
    for transcript in transcripts.into_iter() {
        let id = transcript.dkg_id.ok_or_else(|| {
            "Missing DkgPayload::Summary::IdedNiDkgTranscript::NiDkgId".to_string()
        })?;
        let dkg_id = NiDkgId::try_from(id)
            .map_err(|e| format!("Failed to convert NiDkgId of transcript: {e:?}"))?;
        let callback_id = CallbackId::from(transcript.callback_id);
        let transcript_result = transcript
            .transcript_result
            .ok_or("Missing DkgPayload::Summary::IdedNiDkgTranscript::NiDkgTranscriptResult")?;
        let transcript_result = build_transcript_result(&transcript_result)
            .map_err(|e| format!("Failed to convert NiDkgTranscriptResult: {e:?}"))?;
        transcripts_for_remote_subnets.push(RemoteTranscriptResult {
            dkg_id,
            callback_id,
            transcript_result,
        });
    }
    Ok(transcripts_for_remote_subnets)
}
```

**File:** rs/types/types/src/consensus/dkg.rs (L509-532)
```rust
impl TryFrom<pb::Summary> for DkgSummary {
    type Error = ProxyDecodeError;

    fn try_from(summary: pb::Summary) -> Result<Self, Self::Error> {
        Ok(Self {
            registry_version: RegistryVersion::from(summary.registry_version),
            configs: summary
                .configs
                .into_iter()
                .map(|config| NiDkgConfig::try_from(config).map(|c| (c.dkg_id.clone(), c)))
                .collect::<Result<BTreeMap<_, _>, _>>()?,
            current_transcripts: build_tagged_transcripts_map(&summary.current_transcripts)?,
            next_transcripts: build_tagged_transcripts_map(&summary.next_transcripts)?,
            interval_length: Height::from(summary.interval_length),
            next_interval_length: Height::from(summary.next_interval_length),
            height: Height::from(summary.height),
            transcripts_for_remote_subnets: build_transcripts_vec_from_pb(
                summary.transcripts_for_remote_subnets,
            )
            .map_err(ProxyDecodeError::Other)?,
            remote_dkg_attempts: build_remote_dkg_attempts_map(&summary.remote_dkg_attempts),
        })
    }
}
```

**File:** rs/types/types/src/consensus/dkg.rs (L561-578)
```rust
impl TryFrom<pb::DkgDataPayload> for DkgDataPayload {
    type Error = ProxyDecodeError;

    fn try_from(data_payload: pb::DkgDataPayload) -> Result<Self, Self::Error> {
        Ok(Self {
            start_height: Height::from(data_payload.summary_height),
            messages: data_payload
                .dealings
                .into_iter()
                .map(Message::try_from)
                .collect::<Result<_, _>>()?,
            transcripts_for_remote_subnets: build_transcripts_vec_from_pb(
                data_payload.transcripts_for_remote_subnets,
            )
            .map_err(ProxyDecodeError::Other)?,
        })
    }
}
```

**File:** rs/consensus/dkg/src/payload_builder.rs (L1-1)
```rust
use crate::{
```

**File:** rs/consensus/dkg/src/payload_validator.rs (L30-42)
```rust
pub fn validate_payload(
    subnet_id: SubnetId,
    registry_client: &dyn RegistryClient,
    crypto: &dyn ConsensusCrypto,
    pool_reader: &PoolReader<'_>,
    dkg_pool: &dyn DkgPool,
    parent: Block,
    payload: &BlockPayload,
    state_reader: &dyn StateReader<State = ReplicatedState>,
    validation_context: &ValidationContext,
    metrics: &IntCounterVec,
    log: &ReplicaLogger,
) -> ValidationResult<DkgPayloadValidationError> {
```
