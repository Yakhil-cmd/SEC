Based on my thorough review of the code, I can now provide a definitive assessment.

---

### Title
Unbounded tokio task spawning via crafted `StrippedBlockProposal` ingress/IDKG ID vectors — (`rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs`)

### Summary

A Byzantine peer below the fault threshold can serve a crafted `StrippedBlockProposal` containing an arbitrarily large number of fake `SignedIngressId` or `IDkgArtifactId` entries. `assemble_message` spawns one unbounded tokio task per entry before any size validation, and each task runs an infinite retry loop. This can exhaust heap memory and stall consensus on the victim replica.

### Finding Description

The attack path is concrete and traceable through production code:

**Step 1 — Artifact download with no content-size guard.**

`FetchArtifact::download_artifact` in `rs/p2p/artifact_downloader/src/fetch_artifact/download.rs` downloads the `MaybeStrippedConsensusMessage` from a peer. The only check performed after download is `message.id() == id`: [1](#0-0) 

For a `StrippedBlockProposal`, `id()` is derived solely from `unstripped_consensus_message_id`: [2](#0-1) 

A Byzantine peer can set `unstripped_consensus_message_id` to a legitimately advertised block ID while stuffing `ingress_messages` with 10^5–10^6 fake `SignedIngressId` entries. The ID check passes; the vector length is never checked.

**Step 2 — No size limit on deserialization.**

`TryFrom<pb::StrippedBlockProposal> for StrippedBlockProposal` iterates over `value.ingress_messages` and `value.stripped_idkg_dealings` with no length cap: [3](#0-2) 

The axum router serving these artifacts also explicitly disables body size limits: [4](#0-3) 

**Step 3 — One tokio task spawned per entry, no cap.**

`assemble_message` calls `missing_stripped_messages()` and immediately spawns one `JoinSet` task per returned ID: [5](#0-4) 

`missing_stripped_messages()` returns every entry from both vectors unconditionally: [6](#0-5) 

**Step 4 — Each spawned task runs an infinite retry loop.**

`download_stripped_message` loops forever (`with_max_elapsed_time(None)`) with no internal cancellation: [7](#0-6) 

**Step 5 — Bouncer cancellation is too late.**

The bouncer check only runs *after* all tasks are already spawned, inside the join loop: [8](#0-7) 

The bouncer refresh period is 3 seconds. During that window, all N tasks are live and consuming heap.

**Step 6 — Final ID validation is post-hoc.**

`try_assemble` validates the reconstructed block hash only after all tasks complete: [9](#0-8) 

This catches the fraud, but only after the resource damage is done.

### Impact Explanation

Each spawned tokio task holds its own async stack frame, a cloned `StrippedMessageId`, `ConsensusMessageId`, transport handle, logger, metrics arc, and peer receiver. At N = 10^5 tasks, heap consumption is in the hundreds of MB range. At N = 10^6, OOM is plausible on typical replica hardware. Because `download_stripped_message` loops indefinitely, tasks remain live until the `JoinSet` is dropped (bouncer fires). A Byzantine peer can repeat the attack every consensus round, sustaining memory pressure and degrading or halting consensus liveness on the targeted replica.

### Likelihood Explanation

The attacker needs only a single Byzantine node in the same subnet — below the f-fault threshold. No key material, governance majority, or network-level attack is required. The crafted payload is a valid protobuf message with a legitimate `unstripped_consensus_message_id` and an inflated ID vector. The attack is repeatable every block interval.

### Recommendation

1. **Cap vector length before spawning**: After `missing_stripped_messages()` returns, assert the count is within a protocol-defined bound (e.g., `MAX_INGRESS_MESSAGES_PER_BLOCK + MAX_IDKG_DEALINGS_PER_BLOCK`). Return `AssembleResult::Unwanted` if exceeded.
2. **Validate vector length at deserialization**: Enforce the cap inside `TryFrom<pb::StrippedBlockProposal>` so malformed payloads are rejected before they reach `assemble_message`.
3. **Re-enable body size limits**: Remove or scope `DefaultBodyLimit::disable()` to avoid accepting arbitrarily large artifact payloads.

### Proof of Concept

```rust
// Craft a StrippedBlockProposal with N=100_000 fake ingress IDs
// whose unstripped_consensus_message_id matches a legitimately advertised block.
let fake_stripped = pb::StrippedBlockProposal {
    pruned_block_proposal: Some(valid_pruned_proto),
    unstripped_consensus_message_id: Some(real_block_id.into()),
    ingress_messages: (0..100_000)
        .map(|i| pb::StrippedIngressMessage {
            stripped: Some(fake_ingress_message_id(i)),
            ingress_bytes_hash: vec![i as u8; 32],
        })
        .collect(),
    stripped_idkg_dealings: vec![],
};
// Serve this from the Byzantine peer's /strippedconsensus/rpc endpoint.
// The victim's FetchArtifact passes the id() == id check.
// assemble_message spawns 100_000 tasks, each looping forever in download_stripped_message.
// Heap grows by hundreds of MB; consensus stalls.
```

### Citations

**File:** rs/p2p/artifact_downloader/src/fetch_artifact/download.rs (L44-53)
```rust
fn build_axum_router<Artifact: PbArtifact>(pool: ValidatedPoolReaderRef<Artifact>) -> Router {
    Router::new()
        .route(
            &format!("/{}/rpc", uri_prefix::<Artifact>()),
            any(rpc_handler),
        )
        .with_state(pool)
        // Disable request size limit since consensus might push artifacts larger than limit.
        .layer(DefaultBodyLimit::disable())
}
```

**File:** rs/p2p/artifact_downloader/src/fetch_artifact/download.rs (L250-265)
```rust
                            Ok(Ok(response)) if response.status() == StatusCode::OK => {
                                let body = response.into_body();
                                if let Ok(message) = Artifact::PbMessage::proxy_decode(&body) {
                                    if message.id() == id {
                                        break AssembleResult::Done {
                                            message,
                                            peer_id: peer,
                                        };
                                    } else {
                                        warn!(
                                            log,
                                            "Peer {} responded with wrong artifact for advert",
                                            peer
                                        );
                                    }
                                }
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/types/stripped.rs (L100-130)
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
            unstripped_consensus_message_id,
            stripped_idkg_dealings: StrippedIDkgDealings {
                stripped_dealings: value
                    .stripped_idkg_dealings
                    .into_iter()
                    .map(|dealing| {
                        let idkg_artifact_id: IDkgArtifactId = try_from_option_field(
                            dealing.dealing_id,
                            "StrippedIDkgDealings::dealing_id",
                        )?;
                        if !matches!(idkg_artifact_id, IDkgArtifactId::Dealing(_, _)) {
                            return Err(ProxyDecodeError::Other(format!(
                                "The stripped IDKG artifact id {:?} is NOT for a dealing",
                                idkg_artifact_id,
                            )));
                        }
                        Ok((dealing.dealer_index, idkg_artifact_id))
                    })
                    .collect::<Result<Vec<_>, ProxyDecodeError>>()?,
            },
        })
    }
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

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L235-241)
```rust
        loop {
            let join_result = tokio::select! {
                _ = bouncer.wait_for(|bouncer| matches!(bouncer(&id), BouncerValue::Unwanted)) => {
                    self.metrics.report_aborted_block_assembly();
                    return AssembleResult::Unwanted;
                }
                join_result = join_set.join_next() => join_result,
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L591-598)
```rust
    pub(crate) fn missing_stripped_messages(&self) -> Vec<StrippedMessageId> {
        let ingress_messages = PayloadAssembler::<SignedIngress>::missing_artifacts(self)
            .map(StrippedMessageId::Ingress);
        let idkg_dealings = PayloadAssembler::<SignedIDkgDealing>::missing_artifacts(self)
            .map(|(node_index, dealing_id)| StrippedMessageId::IDkgDealing(dealing_id, node_index));

        ingress_messages.chain(idkg_dealings).collect()
    }
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L640-648)
```rust
        let assembled_id = reconstructed_block_proposal.get_id();
        if assembled_id != claimed_id {
            return Err(AssemblyError::MismatchedConsensusMessageId {
                claimed: claimed_id,
                assembled: assembled_id,
            });
        }

        Ok(reconstructed_block_proposal)
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/download.rs (L301-305)
```rust
    let mut artifact_download_timeout = ExponentialBackoffBuilder::new()
        .with_initial_interval(MIN_ARTIFACT_RPC_TIMEOUT)
        .with_max_interval(MAX_ARTIFACT_RPC_TIMEOUT)
        .with_max_elapsed_time(None)
        .build();
```
