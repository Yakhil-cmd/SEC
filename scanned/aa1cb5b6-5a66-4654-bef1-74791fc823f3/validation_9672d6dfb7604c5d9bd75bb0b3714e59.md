Looking at the code carefully to trace the full attack path and check for any guards.

### Title
Unbounded Tokio Task Spawning via Malicious `StrippedBlockProposal` Ingress Count — (`rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs`)

### Summary

A Byzantine P2P peer can craft a `StrippedBlockProposal` containing an arbitrarily large `ingress_messages` list. Because no count bound is enforced during deserialization or before task spawning, `assemble_message` will spawn one `get_or_fetch` Tokio task per entry — with no upper limit — exhausting the victim replica's thread pool and memory.

### Finding Description

The deserialization of `StrippedBlockProposal` in `TryFrom<pb::StrippedBlockProposal> for StrippedBlockProposal` performs several structural checks but **never validates the length of `ingress_messages`**: [1](#0-0) 

The only guards present are: `pruned_block_proposal` must exist, the ingress payload inside it must be `None`, the `unstripped_consensus_message_id` must be for a block proposal, and each IDKG dealing must be of type `Dealing`. There is no check that `ingress_messages.len() <= MAX_INGRESS_MESSAGES_PER_BLOCK`.

After deserialization, `assemble_message` calls `assembler.missing_stripped_messages()` and spawns one `get_or_fetch` task per entry in an unbounded `for` loop: [2](#0-1) 

The code's own comment at line 441 acknowledges the expected maximum of 1000 entries but does not enforce it: [3](#0-2) 

The protocol-level limit `MAX_INGRESS_MESSAGES_PER_BLOCK = 1000` is defined in `rs/limits/src/lib.rs`: [4](#0-3) 

and enforced in `ingress_selector.rs` during full block validation: [5](#0-4) 

But this validation is **never applied** to the stripped artifact during P2P assembly.

The artifact ID for a `StrippedBlockProposal` is simply its `unstripped_consensus_message_id`: [6](#0-5) 

This means an attacker can craft a `StrippedBlockProposal` with a valid `unstripped_consensus_message_id` (copied from a real advertised block) but with N fake ingress IDs, and the ID check in `FetchArtifact::download_artifact` will pass: [7](#0-6) 

Each spawned `get_or_fetch` task that fails the local pool lookup calls `download_stripped_message`, which immediately increments `active_stripped_message_downloads` and enters an infinite retry loop: [8](#0-7) 

The `try_assemble()` hash mismatch check at the end would eventually reject the block, but only after all N tasks have been spawned and are running.

### Impact Explanation

A single Byzantine subnet node can cause resource exhaustion on any victim replica it is connected to. With each `StrippedIngressMessage` being ~80 bytes in protobuf, even a 4 MB message (matching `MAX_BLOCK_PAYLOAD_SIZE`) yields ~50,000 fake ingress IDs and thus ~50,000 concurrent Tokio tasks, each holding references to the ingress pool, IDKG pool, transport, and metrics, and each looping indefinitely. This degrades or halts the victim replica's ability to participate in consensus.

### Likelihood Explanation

The attacker must be a valid P2P peer (a subnet node). A single Byzantine node below the consensus fault threshold can execute this attack without breaking consensus on other replicas. The attack requires only observing a real block proposal ID (publicly broadcast) and crafting a malicious `StrippedBlockProposal` with that ID.

### Recommendation

Add a count bound check in `TryFrom<pb::StrippedBlockProposal> for StrippedBlockProposal` immediately after deserializing `ingress_messages`:

```rust
if ingress_messages.len() > MAX_INGRESS_MESSAGES_PER_BLOCK as usize {
    return Err(ProxyDecodeError::Other(format!(
        "Too many ingress messages: {} > {}",
        ingress_messages.len(), MAX_INGRESS_MESSAGES_PER_BLOCK
    )));
}
``` [1](#0-0) 

Similarly, add a defensive check before the `for` loop in `assemble_message` that returns `AssembleResult::Unwanted` if the count exceeds the protocol limit.

### Proof of Concept

1. Byzantine node observes a real `StrippedConsensusMessageId` being advertised.
2. Constructs a `pb::StrippedBlockProposal` with the same `unstripped_consensus_message_id` but with N=50,000 `StrippedIngressMessage` entries containing random fake IDs.
3. Pushes or advertises this artifact to the victim replica.
4. Victim's `assemble_message` calls `missing_stripped_messages()` → returns 50,000 entries → spawns 50,000 `get_or_fetch` tasks.
5. Each task fails the local pool lookup and enters `download_stripped_message`, incrementing `active_stripped_message_downloads` 50,000 times and looping indefinitely.
6. Assert: `active_stripped_message_downloads` gauge exceeds 1000; Tokio task count spikes; replica memory and CPU are exhausted.

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

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/types/stripped.rs (L210-219)
```rust
    fn id(&self) -> Self::Id {
        let unstripped_id = match self {
            MaybeStrippedConsensusMessage::Unstripped(unstripped) => unstripped.id(),
            MaybeStrippedConsensusMessage::StrippedBlockProposal(stripped) => {
                stripped.unstripped_consensus_message_id.clone()
            }
        };

        StrippedConsensusMessageId(unstripped_id)
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

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L440-442)
```rust
        // We can have at most 1000 elements in the vector, so it should be reasonably fast to do a
        // linear scan here.
        let (_, ingress) = self
```

**File:** rs/limits/src/lib.rs (L78-78)
```rust
pub const MAX_INGRESS_MESSAGES_PER_BLOCK: u64 = 1000;
```

**File:** rs/ingress_manager/src/ingress_selector.rs (L373-380)
```rust
        if payload.message_count() > settings.max_ingress_messages_per_block {
            return Err(ValidationError::InvalidArtifact(
                InvalidIngressPayloadReason::IngressPayloadTooManyMessages(
                    payload.message_count(),
                    settings.max_ingress_messages_per_block,
                ),
            ));
        }
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/download.rs (L291-305)
```rust
pub(crate) async fn download_stripped_message<P: Peers>(
    transport: Arc<dyn Transport>,
    stripped_message_id: StrippedMessageId,
    block_proposal_id: ConsensusMessageId,
    log: &ReplicaLogger,
    metrics: &FetchStrippedConsensusArtifactMetrics,
    peer_rx: P,
) -> (StrippedMessage, NodeId) {
    let message_type = StrippedMessageType::from(&stripped_message_id);
    metrics.report_started_stripped_message_download(message_type);
    let mut artifact_download_timeout = ExponentialBackoffBuilder::new()
        .with_initial_interval(MIN_ARTIFACT_RPC_TIMEOUT)
        .with_max_interval(MAX_ARTIFACT_RPC_TIMEOUT)
        .with_max_elapsed_time(None)
        .build();
```
