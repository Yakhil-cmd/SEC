### Title
Unbounded `ingress_messages` in `TryFrom<pb::StrippedBlockProposal>` Allows Byzantine Peer to Exhaust Replica Memory and Saturate Tokio Task Scheduler — (`rs/p2p/artifact_downloader/src/fetch_stripped_artifact/types/stripped.rs`)

---

### Summary

A Byzantine peer below the consensus fault threshold can advertise a valid `StrippedConsensusMessageId` and respond with a crafted `StrippedBlockProposal` containing an arbitrarily large `ingress_messages` repeated field. Because `TryFrom<pb::StrippedBlockProposal>` performs no upper-bound check on the count of ingress message IDs, `BlockProposalAssembler::new` allocates a `Vec` of size N, and `assemble_message` subsequently spawns N concurrent `get_or_fetch` Tokio tasks — one per entry — each of which loops indefinitely. This violates the invariant that per-block resource consumption is bounded by the honest block's ingress payload limit, and can cause replica-level memory exhaustion or task-scheduler saturation.

---

### Finding Description

**Step 1 — No count bound in deserialization.**

`TryFrom<pb::StrippedBlockProposal>` in `stripped.rs` validates:
- that `pruned_block_proposal` is present,
- that the pruned block's ingress payload is empty,
- that `unstripped_consensus_message_id` is a `BlockProposal` hash.

It does **not** check `ingress_messages.len()`:

```rust
stripped_ingress_payload: StrippedIngressPayload {
    ingress_messages: value
        .ingress_messages
        .into_iter()
        .map(SignedIngressId::try_from)
        .collect::<Result<Vec<_>, _>>()?,
},
``` [1](#0-0) 

**Step 2 — `BlockProposalAssembler::new` allocates proportionally.**

`BlockProposalAssembler::new` maps every `SignedIngressId` in the stripped payload into a `Vec<(SignedIngressId, Option<SignedIngress>)>` with no cap:

```rust
ingress_messages: stripped_block_proposal
    .stripped_ingress_payload
    .ingress_messages
    .iter()
    .map(|signed_ingress_id| (signed_ingress_id.clone(), None))
    .collect(),
``` [2](#0-1) 

**Step 3 — One Tokio task spawned per entry.**

`assemble_message` calls `assembler.missing_stripped_messages()` (which returns all N entries since none are in the local pool) and spawns a `get_or_fetch` task for each:

```rust
for stripped_message_id in stripped_message_ids {
    join_set.spawn(get_or_fetch(
        stripped_message_id, ...
    ));
}
``` [3](#0-2) 

**Step 4 — Each spawned task loops indefinitely.**

`download_stripped_message` retries with exponential backoff but `with_max_elapsed_time(None)`, meaning each of the N tasks runs forever until the `JoinSet` is dropped (which only happens when the bouncer fires `Unwanted` — but by then N tasks are already consuming memory and scheduler slots). [4](#0-3) 

**Step 5 — The ID check does not constrain content.**

`FetchArtifact::download_artifact` checks `message.id() == id` after downloading. For `MaybeStrippedConsensusMessage`, the ID is derived solely from `unstripped_consensus_message_id` (the block proposal hash). A Byzantine peer can set this field to a valid, currently-wanted block proposal hash while inflating `ingress_messages` to an arbitrary count — the ID check passes. [5](#0-4) 

**Step 6 — Bouncer only checks the artifact ID, not content.**

The `BouncerFactoryWrapper` delegates to the consensus pool bouncer, which evaluates only the `ConsensusMessageId` (height + hash). It has no visibility into the number of ingress message IDs embedded in the stripped payload. [6](#0-5) 

---

### Impact Explanation

A single Byzantine peer can trigger allocation of a `Vec` with up to ~40,000–80,000 entries (within a typical 4 MB QUIC message) and spawn an equal number of Tokio tasks per targeted block proposal. Each task holds a stack frame and loops indefinitely. With multiple concurrent block proposals in flight (the bouncer allows up to `LOOK_AHEAD = 10` heights), the total task count multiplies. This can cause:

- **Memory exhaustion**: Vec allocation + Tokio task overhead per entry.
- **Task-scheduler saturation**: The Tokio runtime's thread pool is flooded with blocked async tasks, starving legitimate consensus work.

The impact is scoped to a single replica (non-volumetric, single-peer trigger), matching the stated scope. [7](#0-6) 

---

### Likelihood Explanation

The attacker needs only to be a subnet peer (below the Byzantine fault threshold) and to observe a block proposal ID that the victim is currently downloading — both are trivially achievable in normal subnet operation. No privileged access, key material, or majority corruption is required. The crafted message passes all existing validation gates before the resource allocation occurs. [8](#0-7) 

---

### Recommendation

Add an upper-bound check on `ingress_messages.len()` (and `stripped_idkg_dealings.len()`) inside `TryFrom<pb::StrippedBlockProposal>`, before the `collect()` call. The bound should be the protocol-enforced maximum: `max_ingress_messages_per_block` (registry parameter, default 1000) for ingress, and `dkg_dealings_per_block` for IDKG dealings. Reject the artifact with `ProxyDecodeError` if either count exceeds its respective limit. [9](#0-8) 

---

### Proof of Concept

```rust
// Craft a StrippedBlockProposal proto with N fake ingress_messages entries.
let mut proto = pb::StrippedBlockProposal {
    pruned_block_proposal: Some(pb::BlockProposal::default()),
    unstripped_consensus_message_id: Some(/* valid wanted block proposal ID */),
    ingress_messages: (0..100_000)
        .map(|i| pb::StrippedIngressMessage {
            stripped: Some(pb::IngressMessageId {
                expiry: 0,
                message_id: vec![i as u8; 32],
            }),
            ingress_bytes_hash: vec![0u8; 32],
        })
        .collect(),
    stripped_idkg_dealings: vec![],
};

// Serve this proto in response to the victim's RPC for the wanted block proposal ID.
// Observe: BlockProposalAssembler::new allocates a Vec of 100,000 entries,
// assemble_message spawns 100,000 get_or_fetch tasks, each looping indefinitely.
// Assert: memory usage and spawned task count are NOT bounded before the bouncer fires.
``` [10](#0-9)

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

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L86-103)
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

    fn refresh_period(&self) -> Duration {
        Duration::from_secs(3)
    }
}
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L203-226)
```rust
        let mut join_set = tokio::task::JoinSet::new();

        let timer = self
            .metrics
            .download_missing_stripped_messages_duration
            .start_timer();
        let mut assembler = BlockProposalAssembler::new(stripped_block_proposal);

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

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/download.rs (L301-306)
```rust
    let mut artifact_download_timeout = ExponentialBackoffBuilder::new()
        .with_initial_interval(MIN_ARTIFACT_RPC_TIMEOUT)
        .with_max_interval(MAX_ARTIFACT_RPC_TIMEOUT)
        .with_max_elapsed_time(None)
        .build();

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
