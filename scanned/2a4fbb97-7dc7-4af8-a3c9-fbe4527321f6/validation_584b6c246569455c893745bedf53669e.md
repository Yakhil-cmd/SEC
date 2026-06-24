### Title
Unbounded Tokio Task Spawning via Attacker-Controlled `stripped_idkg_dealings` Count in `assemble_message` — (`rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs`)

---

### Summary

A Byzantine peer below the fault threshold can craft a `StrippedBlockProposal` with an arbitrarily large `stripped_idkg_dealings` list. Because `assemble_message` spawns one unbounded tokio task per entry with no concurrency cap, and each task enters an infinite retry loop, the attacker can transiently exhaust the tokio thread pool and transport connection pool for the duration of the consensus round.

---

### Finding Description

**Step 1 — No size guard on deserialization.**

`TryFrom<pb::StrippedBlockProposal> for StrippedBlockProposal` iterates over `value.stripped_idkg_dealings` with no count check: [1](#0-0) 

There is no equivalent of the NiDKG `TooManyDealings` guard (which only covers `NiDkgMessage`, not `IDkgArtifactId::Dealing`): [2](#0-1) 

The system constant `DKG_DEALINGS_PER_BLOCK = 1` applies exclusively to NiDKG, not to IDKG chain-key dealings: [3](#0-2) 

**Step 2 — Unbounded `JoinSet` spawn.**

For every entry returned by `missing_stripped_messages()`, `assemble_message` unconditionally spawns a task with no concurrency limit: [4](#0-3) 

**Step 3 — Infinite retry loop per task.**

`download_stripped_message` is configured with `with_max_elapsed_time(None)` and loops forever until a successful response: [5](#0-4) 

**Step 4 — Bouncer is the only cleanup path.**

Tasks are aborted only when the bouncer marks the block `Unwanted` (i.e., when the height is finalized). The bouncer refresh period is 3 seconds: [6](#0-5) 

The consensus bouncer marks a `BlockProposal` as `Wants` while `finalized_height < height <= notarized_height + LOOK_AHEAD (10)`: [7](#0-6) 

---

### Impact Explanation

During the window between when the Byzantine block is advertised and when its height is finalized (typically several seconds, up to tens of seconds if consensus is slow), N tasks — one per crafted `stripped_idkg_dealings` entry — each hold a tokio task slot and issue repeated transport RPCs. With N in the thousands (protobuf imposes no per-field count limit), this can saturate the tokio worker thread pool and the QUIC connection pool, degrading the replica's ability to process other P2P messages, consensus artifacts, and ingress.

---

### Likelihood Explanation

The attacker must be a valid block maker for the current round (a Byzantine node below the fault threshold). Such a node can:
1. Produce a legitimate `BlockProposal` (so `unstripped_consensus_message_id` passes the bouncer).
2. Craft a `StrippedBlockProposal` with the correct block hash but an inflated `stripped_idkg_dealings` list.
3. Advertise it to victim peers.

The `try_assemble()` ID-mismatch check fires only after all tasks complete or the bouncer fires — by which time the resource exhaustion has already occurred. [8](#0-7) 

---

### Recommendation

Add a maximum-count guard during deserialization of `StrippedBlockProposal`, analogous to the NiDKG `TooManyDealings` check. A reasonable bound is the subnet size (number of nodes), since each transcript can have at most one dealing per dealer. Reject any `StrippedBlockProposal` whose `stripped_idkg_dealings.len()` exceeds this bound before any tasks are spawned.

Optionally, cap the `JoinSet` concurrency in `assemble_message` with a semaphore as a defense-in-depth measure.

---

### Proof of Concept

```rust
// Craft a StrippedBlockProposal with 1000 fake stripped_idkg_dealings entries.
// The unstripped_consensus_message_id is a valid block hash at the current height
// (from the Byzantine node's own legitimate block proposal).
// Advertise it to a victim replica.
// Observe: 1000 tokio tasks spawned, each retrying transport RPCs indefinitely,
// until the height is finalized (~seconds later).
// Measure: tokio task count spikes to 1000+; transport connection pool saturated.
```

The deserialization path that accepts the inflated list without any count check: [9](#0-8)

### Citations

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/types/stripped.rs (L67-130)
```rust
impl TryFrom<pb::StrippedBlockProposal> for StrippedBlockProposal {
    type Error = ProxyDecodeError;

    fn try_from(value: pb::StrippedBlockProposal) -> Result<Self, Self::Error> {
        let pruned_block_proposal_proto = value
            .pruned_block_proposal
            .ok_or_else(|| ProxyDecodeError::MissingField("pruned_block_proposal"))?;

        if pruned_block_proposal_proto
            .value
            .as_ref()
            .is_some_and(|block| block.ingress_payload.is_some())
        {
            return Err(ProxyDecodeError::Other(String::from(
                "The ingress payload is NOT empty",
            )));
        }

        let unstripped_consensus_message_id: ConsensusMessageId = try_from_option_field(
            value.unstripped_consensus_message_id,
            "unstripped_consensus_message_id",
        )?;

        if !matches!(
            unstripped_consensus_message_id.hash,
            ConsensusMessageHash::BlockProposal(_)
        ) {
            return Err(ProxyDecodeError::Other(format!(
                "The unstripped consensus message id {:?} is NOT for a block proposal",
                unstripped_consensus_message_id,
            )));
        }

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

**File:** rs/consensus/dkg/src/payload_validator.rs (L164-170)
```rust
    if dealings.messages.len() > max_dealings_per_payload {
        return Err(InvalidDkgPayloadReason::TooManyDealings {
            limit: max_dealings_per_payload,
            actual: dealings.messages.len(),
        }
        .into());
    }
```

**File:** rs/limits/src/lib.rs (L91-93)
```rust
/// The default upper bound for the number of allowed dkg dealings in a
/// block.
pub const DKG_DEALINGS_PER_BLOCK: usize = 1;
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

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L620-649)
```rust
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

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/download.rs (L301-376)
```rust
    let mut artifact_download_timeout = ExponentialBackoffBuilder::new()
        .with_initial_interval(MIN_ARTIFACT_RPC_TIMEOUT)
        .with_max_interval(MAX_ARTIFACT_RPC_TIMEOUT)
        .with_max_elapsed_time(None)
        .build();

    let mut rng = SmallRng::from_entropy();

    let request = match &stripped_message_id {
        StrippedMessageId::Ingress(signed_ingress_id) => {
            let request = GetIngressMessageInBlockRequest {
                signed_ingress_id: signed_ingress_id.clone(),
                block_proposal_id,
            };
            let bytes = Bytes::from(pb::GetIngressMessageInBlockRequest::proxy_encode(request));
            Request::builder().uri(INGRESS_URI).body(bytes).unwrap()
        }
        StrippedMessageId::IDkgDealing(dealing_id, node_index) => {
            let request = GetIDkgDealingInBlockRequest {
                node_index: *node_index,
                dealing_id: dealing_id.clone(),
                block_proposal_id,
            };
            let bytes = Bytes::from(pb::GetIDkgDealingInBlockRequest::proxy_encode(request));
            Request::builder()
                .uri(IDKG_DEALING_URI)
                .body(bytes)
                .unwrap()
        }
    };

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
}
```

**File:** rs/consensus/src/consensus/priority.rs (L83-96)
```rust
        ConsensusMessageHash::Notarization(_)
        | ConsensusMessageHash::Finalization(_)
        | ConsensusMessageHash::FinalizationShare(_)
        | ConsensusMessageHash::BlockProposal(_)
        | ConsensusMessageHash::EquivocationProof(_) => {
            // Ignore finalized
            if height <= finalized_height {
                Unwanted
            } else if height <= notarized_height + Height::from(LOOK_AHEAD) {
                Wants
            } else {
                MaybeWantsLater
            }
        }
```
