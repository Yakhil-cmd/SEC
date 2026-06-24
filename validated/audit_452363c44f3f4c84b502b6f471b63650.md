Audit Report

## Title
Unbounded Tokio Task Spawning via Oversized `ingress_messages` in `StrippedBlockProposal` — (`rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs`)

## Summary

`TryFrom<pb::StrippedBlockProposal> for StrippedBlockProposal` performs several structural checks but never validates the length of the `ingress_messages` field. A Byzantine P2P peer can craft a `StrippedBlockProposal` with a valid `unstripped_consensus_message_id` (copied from a real advertised block) but with an arbitrarily large `ingress_messages` list. `assemble_message` then spawns one unbounded `get_or_fetch` Tokio task per entry, each entering an infinite retry loop, exhausting the victim replica's task pool and memory.

## Finding Description

**Root cause — missing length bound in deserialization:**

`TryFrom<pb::StrippedBlockProposal> for StrippedBlockProposal` (lines 67–130 of `stripped.rs`) validates that `pruned_block_proposal` exists, that the ingress payload inside it is `None`, that `unstripped_consensus_message_id` is for a block proposal, and that each IDKG dealing is of type `Dealing`. It never checks `ingress_messages.len()`: [1](#0-0) 

The protocol constant `MAX_INGRESS_MESSAGES_PER_BLOCK = 1000` exists: [2](#0-1) 

but is never applied here.

**Unbounded task spawning:**

After deserialization, `assemble_message` calls `assembler.missing_stripped_messages()` and spawns one `get_or_fetch` task per entry with no upper-bound check: [3](#0-2) 

The code's own comment at line 440 acknowledges the expected maximum of 1000 entries but does not enforce it: [4](#0-3) 

**Infinite retry loop per task:**

Each `get_or_fetch` task that misses the local pool calls `download_stripped_message`, which immediately increments `active_stripped_message_downloads` and enters an infinite retry loop (`with_max_elapsed_time(None)`): [5](#0-4) [6](#0-5) 

The gauge is only decremented on a successful download: [7](#0-6) 

**Why the bouncer check is insufficient:**

The bouncer abort path (lines 233–242) only fires *after* all N tasks have already been spawned. The `join_set` drop on `Unwanted` eventually aborts the tasks, but the window between spawning and cleanup (up to the 3-second bouncer refresh period) allows all N tasks to run concurrently: [8](#0-7) 

Furthermore, the attack is repeatable: a new block proposal is advertised roughly every second, so the attacker can trigger a fresh wave of N tasks per round, accumulating tasks faster than the bouncer cleans them up.

**ID check bypass:**

The artifact ID for a `StrippedBlockProposal` is simply its `unstripped_consensus_message_id`: [9](#0-8) 

The `FetchArtifact::download_artifact` ID check (`message.id() == id`) passes as long as the attacker copies a real block proposal's ID, regardless of how many fake ingress IDs are embedded in the payload: [10](#0-9) 

## Impact Explanation

A single Byzantine subnet node below the consensus fault threshold can cause resource exhaustion on any victim replica it is connected to. With `MAX_INGRESS_MESSAGES_PER_BLOCK = 1000` being the expected maximum, an attacker can craft a payload with orders of magnitude more entries (protobuf has no field-count limit). Each spawned task holds references to the ingress pool, IDKG pool, transport, and metrics, and loops indefinitely with exponential backoff. Repeated across consecutive block rounds, this degrades or halts the victim replica's ability to participate in consensus.

This matches the **High** allowed impact: *"Application/platform-level DoS, crash, consensus blocking, certified-state disruption, or subnet availability impact not based on raw volumetric DDoS."*

## Likelihood Explanation

The attacker must be a valid P2P peer (a subnet node). A single Byzantine node below the consensus fault threshold can execute this attack without breaking consensus on other replicas. The attack requires only observing a real block proposal ID (publicly broadcast) and crafting a malicious `StrippedBlockProposal` with that ID. The attack is repeatable every consensus round (~1 second), making sustained resource exhaustion feasible.

## Recommendation

Add a count bound check in `TryFrom<pb::StrippedBlockProposal> for StrippedBlockProposal` immediately after deserializing `ingress_messages`:

```rust
if ingress_messages.len() > MAX_INGRESS_MESSAGES_PER_BLOCK as usize {
    return Err(ProxyDecodeError::Other(format!(
        "Too many ingress messages: {} > {}",
        ingress_messages.len(), MAX_INGRESS_MESSAGES_PER_BLOCK
    )));
}
```

Similarly, add a defensive check before the `for` loop in `assemble_message` that returns `AssembleResult::Unwanted` if the count exceeds the protocol limit. Apply the same bound to `stripped_idkg_dealings`.

## Proof of Concept

1. Byzantine node observes a real `StrippedConsensusMessageId` being advertised for the current round.
2. Constructs a `pb::StrippedBlockProposal` with the same `unstripped_consensus_message_id` but with N >> 1000 `StrippedIngressMessage` entries containing random fake IDs.
3. Serves this artifact when the victim's `FetchArtifact::download_artifact` requests it via RPC.
4. Victim's `assemble_message` calls `missing_stripped_messages()` → returns N entries → spawns N `get_or_fetch` tasks.
5. Each task fails the local pool lookup and enters `download_stripped_message`, incrementing `active_stripped_message_downloads` N times and looping indefinitely.
6. Repeat each round. Assert: `ic_stripped_consensus_artifact_active_stripped_message_downloads` gauge grows unboundedly; Tokio task count spikes; replica memory and CPU are exhausted.
7. A deterministic unit test can be written by constructing a `StrippedBlockProposal` with N > 1000 fake ingress IDs directly (bypassing the network layer) and asserting that `assemble_message` either rejects it early or that the spawned task count is bounded.

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

**File:** rs/limits/src/lib.rs (L78-78)
```rust
pub const MAX_INGRESS_MESSAGES_PER_BLOCK: u64 = 1000;
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

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L233-242)
```rust
        let mut bouncer = self.fetch_stripped.bouncer_watcher();

        loop {
            let join_result = tokio::select! {
                _ = bouncer.wait_for(|bouncer| matches!(bouncer(&id), BouncerValue::Unwanted)) => {
                    self.metrics.report_aborted_block_assembly();
                    return AssembleResult::Unwanted;
                }
                join_result = join_set.join_next() => join_result,
            };
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L440-442)
```rust
        // We can have at most 1000 elements in the vector, so it should be reasonably fast to do a
        // linear scan here.
        let (_, ingress) = self
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/download.rs (L299-305)
```rust
    let message_type = StrippedMessageType::from(&stripped_message_id);
    metrics.report_started_stripped_message_download(message_type);
    let mut artifact_download_timeout = ExponentialBackoffBuilder::new()
        .with_initial_interval(MIN_ARTIFACT_RPC_TIMEOUT)
        .with_max_interval(MAX_ARTIFACT_RPC_TIMEOUT)
        .with_max_elapsed_time(None)
        .build();
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/download.rs (L332-375)
```rust
    loop {
        let next_request_at = Instant::now()
            + artifact_download_timeout
                .next_backoff()
                .unwrap_or(MAX_ARTIFACT_RPC_TIMEOUT);
        if let Some(peer) = { peer_rx.peers().into_iter().choose(&mut rng) } {
            match timeout_at(next_request_at, transport.rpc(&peer, request.clone())).await {
                Ok(Ok(response)) if response.status() == StatusCode::OK => {
                    match parse_response(&stripped_message_id, response.into_body()) {
                        Ok(stripped_message) => {
                            metrics.report_finished_stripped_message_download(message_type);
                            return (stripped_message, peer);
                        }
                        Err(ParseResponseError::MessageIdMismatch) => {
                            metrics.report_download_error(
                                "mismatched_stripped_message_id",
                                message_type,
                            );
                            warn!(
                                log,
                                "Peer {} responded with wrong {} message for advert",
                                peer,
                                message_type.as_str(),
                            );
                        }
                        Err(ParseResponseError::ParsingError(reason)) => {
                            metrics.report_download_error(reason, message_type);
                        }
                    };
                }
                Ok(Ok(_response)) => {
                    metrics.report_download_error("status_not_ok", message_type);
                }
                Ok(Err(_rpc_error)) => {
                    metrics.report_download_error("rpc_error", message_type);
                }
                Err(_timeout) => {
                    metrics.report_download_error("timeout", message_type);
                }
            }
        }

        sleep_until(next_request_at).await;
    }
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/metrics.rs (L112-128)
```rust
    pub(super) fn report_started_stripped_message_download(
        &self,
        message_type: StrippedMessageType,
    ) {
        self.active_stripped_message_downloads
            .with_label_values(&[message_type.as_str()])
            .inc()
    }

    pub(super) fn report_finished_stripped_message_download(
        &self,
        message_type: StrippedMessageType,
    ) {
        self.active_stripped_message_downloads
            .with_label_values(&[message_type.as_str()])
            .dec()
    }
```

**File:** rs/p2p/artifact_downloader/src/fetch_artifact/download.rs (L252-258)
```rust
                                if let Ok(message) = Artifact::PbMessage::proxy_decode(&body) {
                                    if message.id() == id {
                                        break AssembleResult::Done {
                                            message,
                                            peer_id: peer,
                                        };
                                    } else {
```
