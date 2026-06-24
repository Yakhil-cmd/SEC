Audit Report

## Title
Unbounded `ingress_messages` deserialization in `TryFrom<pb::StrippedBlockProposal>` enables Byzantine peer to exhaust replica memory and tokio task capacity — (`rs/p2p/artifact_downloader/src/fetch_stripped_artifact/types/stripped.rs`)

## Summary
The `TryFrom<pb::StrippedBlockProposal>` implementation collects the attacker-controlled `ingress_messages` array into a `Vec<SignedIngressId>` with no upper-bound check, despite the protocol constant `MAX_INGRESS_MESSAGES_PER_BLOCK = 1000` existing in `rs/limits/src/lib.rs`. A Byzantine subnet peer can send a crafted `pb::StrippedBlockProposal` with an arbitrarily large `ingress_messages` array, causing unbounded memory allocation in `BlockProposalAssembler::new` and unbounded tokio task spawning in `assemble_message`, leading to OOM crash or async runtime exhaustion and halting the victim replica's participation in consensus.

## Finding Description

**Root cause — no count bound in `TryFrom<pb::StrippedBlockProposal>`:** [1](#0-0) 

The `ingress_messages` field is iterated and collected into a `Vec<SignedIngressId>` with no length check. The protocol constant `MAX_INGRESS_MESSAGES_PER_BLOCK = 1000` is defined: [2](#0-1) 

but is never imported or consulted in this deserialization path. The developer's intent is explicitly acknowledged in a comment in the assembler: [3](#0-2) 

yet no assertion or early-return enforces it.

**Propagation — `BlockProposalAssembler::new` allocates proportionally:** [4](#0-3) 

The assembler creates a `Vec<(SignedIngressId, Option<SignedIngress>)>` whose length equals the attacker-controlled count, doubling the allocation.

**Explosion — `assemble_message` spawns one tokio task per entry:** [5](#0-4) 

`missing_stripped_messages()` returns all N entries and the loop spawns N concurrent `get_or_fetch` tasks into a `JoinSet` with no cap. This spawn loop runs synchronously before the `select!` loop, so all N tasks are already live before the bouncer abort path can fire: [6](#0-5) 

**The `try_assemble()` ID-mismatch check is too late:** [7](#0-6) 

This check runs only after all tasks complete — the memory and task explosion has already occurred.

**The bouncer validates only the artifact ID, not payload content:** [8](#0-7) 

`BouncerFactoryWrapper` delegates to the consensus bouncer which checks height/hash of the `ConsensusMessageId` only — it never inspects the `ingress_messages` count inside the stripped payload.

**`DefaultBodyLimit::disable()` removes the axum-level size guard on sub-artifact endpoints:** [9](#0-8) 

## Impact Explanation

A Byzantine peer sends a `pb::StrippedBlockProposal` with a valid `unstripped_consensus_message_id` (for a real block the victim is assembling) but with N fake `ingress_messages` entries. Each `SignedIngressId` is ~72 bytes; even within typical QUIC transport message size limits (~10 MB), an attacker can pack ~100,000 fake entries, causing ~14 MB of Vec allocation doubled by the assembler, and spawning ~100,000 concurrent tokio tasks. At higher limits or with repeated attacks, this reaches OOM or async runtime exhaustion. The victim replica halts its participation in consensus, degrading subnet finalization. This matches the allowed High impact: **Application/platform-level DoS, crash, consensus blocking, or subnet availability impact not based on raw volumetric DDoS.**

## Likelihood Explanation

The attacker must be a legitimate subnet peer (Byzantine node below the fault threshold). It must observe a `ConsensusMessageId` that the victim's bouncer will accept as `Wants` — trivially achievable by watching the P2P advert stream, as block proposal IDs are public. The crafted message is syntactically valid protobuf and passes all `TryFrom` checks. No cryptographic material is required to forge the `ingress_messages` array. The attack is repeatable for every block round.

## Recommendation

Add a count bound check immediately in `TryFrom<pb::StrippedBlockProposal>` before collecting into a `Vec`, using the existing `MAX_INGRESS_MESSAGES_PER_BLOCK` constant:

```rust
// In TryFrom<pb::StrippedBlockProposal> for StrippedBlockProposal
use ic_limits::MAX_INGRESS_MESSAGES_PER_BLOCK;
if value.ingress_messages.len() > MAX_INGRESS_MESSAGES_PER_BLOCK as usize {
    return Err(ProxyDecodeError::Other(format!(
        "Too many stripped ingress messages: {} > {}",
        value.ingress_messages.len(), MAX_INGRESS_MESSAGES_PER_BLOCK
    )));
}
```

Apply the same guard to `stripped_idkg_dealings`. As defense-in-depth, also cap the `JoinSet` spawn loop in `assemble_message`.

## Proof of Concept

```rust
// Unit test: construct a pb::StrippedBlockProposal with > 1000 fake ingress entries
// and verify that TryFrom rejects it (currently it does NOT).
let fake_ingress_msg = pb::StrippedIngressMessage {
    stripped: Some(/* any valid IngressMessageId proto */),
    ingress_bytes_hash: vec![0u8; 32],
};
let malicious_proto = pb::StrippedBlockProposal {
    pruned_block_proposal: Some(/* valid empty-ingress block proto */),
    unstripped_consensus_message_id: Some(/* valid block-proposal ConsensusMessageId */),
    ingress_messages: vec![fake_ingress_msg; 2_000],  // exceeds MAX_INGRESS_MESSAGES_PER_BLOCK
    stripped_idkg_dealings: vec![],
};
// Currently succeeds — no count check at lines 100-108 of stripped.rs
let result = StrippedBlockProposal::try_from(malicious_proto);
assert!(result.is_err()); // should fail after fix; currently passes

// Then BlockProposalAssembler::new allocates 2000 entries,
// and assemble_message spawns 2000 tokio tasks.
// Scale to transport limit for OOM / task exhaustion.
```

### Citations

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/types/stripped.rs (L100-108)
```rust
        Ok(Self {
            pruned_block_proposal_proto,
            stripped_ingress_payload: StrippedIngressPayload {
                ingress_messages: value
                    .ingress_messages
                    .into_iter()
                    .map(SignedIngressId::try_from)
                    .collect::<Result<Vec<_>, _>>()?,
            },
```

**File:** rs/limits/src/lib.rs (L78-78)
```rust
pub const MAX_INGRESS_MESSAGES_PER_BLOCK: u64 = 1000;
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L86-98)
```rust
impl<Pool: ValidatedPoolReader<ConsensusMessage>>
    BouncerFactory<StrippedConsensusMessageId, ConsensusPoolWrapper<Pool>>
    for BouncerFactoryWrapper<Pool>
{
    fn new_bouncer(
        &self,
        pool: &ConsensusPoolWrapper<Pool>,
    ) -> ic_interfaces::p2p::consensus::Bouncer<StrippedConsensusMessageId> {
        let pool = pool.consensus_pool.read().unwrap();
        let nested = self.bouncer_factory.new_bouncer(&pool);

        Box::new(move |id| nested(id.as_ref()))
    }
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L211-226)
```rust
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
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L236-240)
```rust
            let join_result = tokio::select! {
                _ = bouncer.wait_for(|bouncer| matches!(bouncer(&id), BouncerValue::Unwanted)) => {
                    self.metrics.report_aborted_block_assembly();
                    return AssembleResult::Unwanted;
                }
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L440-441)
```rust
        // We can have at most 1000 elements in the vector, so it should be reasonably fast to do a
        // linear scan here.
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L572-588)
```rust
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
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L640-646)
```rust
        let assembled_id = reconstructed_block_proposal.get_id();
        if assembled_id != claimed_id {
            return Err(AssemblyError::MismatchedConsensusMessageId {
                claimed: claimed_id,
                assembled: assembled_id,
            });
        }
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/download.rs (L205-212)
```rust
pub(super) fn build_axum_router(pools: Pools) -> Router {
    Router::new()
        .route(INGRESS_URI, any(ingress_rpc_handler))
        .route(IDKG_DEALING_URI, any(idkg_dealing_rpc_handler))
        .with_state(pools)
        // Disable request size limit since consensus might push artifacts larger than limit.
        .layer(DefaultBodyLimit::disable())
}
```
