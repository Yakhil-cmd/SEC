### Title
Unbounded Duplicate `(NodeIndex, IDkgArtifactId)` Entries in `StrippedBlockProposal` Cause N Concurrent Fetch Tasks Without Deduplication — (`rs/p2p/artifact_downloader/src/fetch_stripped_artifact/types/stripped.rs`, `assembler.rs`)

---

### Summary

A Byzantine peer can craft a `pb::StrippedBlockProposal` containing N duplicate `(NodeIndex, IDkgArtifactId)` entries in `stripped_idkg_dealings`. Because neither `TryFrom<pb::StrippedBlockProposal>` nor `BlockProposalAssembler::new()` deduplicate this list, `assemble_message` spawns N concurrent `get_or_fetch` Tokio tasks for the same dealing. Each task that cannot find the dealing locally issues live RPC calls to subnet peers in an unbounded retry loop. The assembly then permanently fails (`AssembleResult::Unwanted`) on the first `AlreadyInserted` collision, meaning the victim replica never assembles the block from this peer and must retry — while the N tasks have already been dispatched.

---

### Finding Description

**1. No deduplication in deserialization (`stripped.rs` lines 110–128)**

`TryFrom<pb::StrippedBlockProposal>` iterates `value.stripped_idkg_dealings` and collects into a plain `Vec` with no uniqueness check:

```rust
stripped_idkg_dealings: StrippedIDkgDealings {
    stripped_dealings: value
        .stripped_idkg_dealings
        .into_iter()
        .map(|dealing| { ... Ok((dealing.dealer_index, idkg_artifact_id)) })
        .collect::<Result<Vec<_>, ProxyDecodeError>>()?,
},
``` [1](#0-0) 

**2. No deduplication in `BlockProposalAssembler::new()` (`assembler.rs` lines 580–585)**

The `signed_dealings` Vec is built directly from the iterator:

```rust
signed_dealings: stripped_block_proposal
    .stripped_idkg_dealings
    .stripped_dealings
    .iter()
    .map(|(node_index, dealing_id)| ((*node_index, dealing_id.clone()), None))
    .collect(),
``` [2](#0-1) 

**3. One task spawned per Vec entry, including duplicates (`assembler.rs` lines 211–226)**

`missing_stripped_messages()` returns all `None`-valued entries — all N duplicates — and `assemble_message` spawns one `get_or_fetch` task per entry before the bouncer is ever consulted:

```rust
let stripped_message_ids = assembler.missing_stripped_messages();
for stripped_message_id in stripped_message_ids {
    join_set.spawn(get_or_fetch(...));
}
// bouncer is only checked AFTER this loop, inside the select! below
let mut bouncer = self.fetch_stripped.bouncer_watcher();
``` [3](#0-2) 

**4. Each task issues live RPC calls in an infinite retry loop (`download.rs` lines 301–375)**

`download_stripped_message` uses `with_max_elapsed_time(None)` — no timeout — and loops forever until a peer responds:

```rust
let mut artifact_download_timeout = ExponentialBackoffBuilder::new()
    .with_max_elapsed_time(None)
    .build();
loop {
    // ... transport.rpc(&peer, request.clone()).await ...
    sleep_until(next_request_at).await;
}
``` [4](#0-3) 

**5. Assembly permanently fails on first duplicate insertion (`assembler.rs` lines 262–271)**

When the second task for the same dealing ID completes, `try_insert` returns `InsertionError::AlreadyInserted`, and `assemble_message` immediately returns `AssembleResult::Unwanted`, dropping the `join_set` (aborting remaining tasks) — but all N tasks were already dispatched and may have already issued RPC calls:

```rust
if let Err(err) = assembler.try_insert_stripped_message(message) {
    warn!(self.log, "Failed to insert stripped message of type {}: {}. This is a bug.", ...);
    return AssembleResult::Unwanted;
}
``` [5](#0-4) 

---

### Impact Explanation

- **Excessive peer RPC load**: For each crafted `StrippedBlockProposal` with N duplicate dealing IDs, the victim replica fires N concurrent `transport.rpc()` calls to subnet peers for the same dealing. With N=1000 and one crafted message per consensus round (~1 s), this is a sustained amplified RPC storm directed at honest peers from a single Byzantine node.
- **Assembly always fails**: The `AlreadyInserted` path always returns `Unwanted`, so the victim replica never successfully assembles the block from the Byzantine peer. It must re-fetch from another peer, adding latency.
- **Scoped to a single replica**: The attack degrades one replica's performance and its outbound RPC load on peers; it does not directly break subnet consensus as long as other replicas are healthy.

---

### Likelihood Explanation

- Requires only a single Byzantine peer (well below the fault threshold).
- The crafted proto is trivially constructed: repeat the same `StrippedIDkgDealing` entry N times in the `stripped_idkg_dealings` repeated field.
- The Byzantine peer advertises a valid `StrippedConsensusMessageId` (pointing to any real block proposal hash), causing the victim to fetch the stripped artifact from it.
- No privileged access, key material, or majority corruption required.

---

### Recommendation

Deduplicate `stripped_idkg_dealings` at the earliest validation boundary. The fix belongs in `TryFrom<pb::StrippedBlockProposal>`:

```rust
let mut seen = std::collections::HashSet::new();
stripped_dealings: value
    .stripped_idkg_dealings
    .into_iter()
    .map(|dealing| { ... Ok((dealing.dealer_index, idkg_artifact_id)) })
    .filter(|Ok((idx, id))| seen.insert((idx, id.clone())))
    .collect::<Result<Vec<_>, ProxyDecodeError>>()?
```

Or, more strictly, return a `ProxyDecodeError` on any duplicate, since a well-formed stripped block proposal produced by an honest node will never contain duplicates. [1](#0-0) 

---

### Proof of Concept

State-machine test sketch:

```rust
// Craft a StrippedBlockProposal with 1000 duplicate dealing entries
let dealing_id = fake_idkg_dealing(NODE_1, 1).id();
let mut stripped = fake_stripped_block_proposal_with_messages(vec![]);
for _ in 0..1000 {
    stripped.stripped_idkg_dealings.stripped_dealings
        .push((1u32, dealing_id.clone()));
}
let assembler = BlockProposalAssembler::new(stripped);
let missing = assembler.missing_stripped_messages();
// Assert: should be 1 (distinct), not 1000
assert_eq!(missing.len(), 1, "Expected deduplication; got {} tasks", missing.len());
``` [6](#0-5)

### Citations

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/types/stripped.rs (L110-128)
```rust
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
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L211-233)
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

        let mut messages_from_pool = BTreeMap::<StrippedMessageType, usize>::new();
        let mut messages_from_peers = BTreeMap::<StrippedMessageType, usize>::new();

        // Abort the assembly as soon as the block proposal is no longer wanted. Returning
        // here drops `join_set`, which aborts all outstanding child fetch tasks.
        let mut bouncer = self.fetch_stripped.bouncer_watcher();
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L262-271)
```rust
            if let Err(err) = assembler.try_insert_stripped_message(message) {
                warn!(
                    self.log,
                    "Failed to insert stripped message of type {}: {}. This is a bug.",
                    message_type.as_str(),
                    err
                );

                return AssembleResult::Unwanted;
            }
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L571-598)
```rust
impl BlockProposalAssembler {
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

    /// Returns the list of messages which have been stripped from the block.
    pub(crate) fn missing_stripped_messages(&self) -> Vec<StrippedMessageId> {
        let ingress_messages = PayloadAssembler::<SignedIngress>::missing_artifacts(self)
            .map(StrippedMessageId::Ingress);
        let idkg_dealings = PayloadAssembler::<SignedIDkgDealing>::missing_artifacts(self)
            .map(|(node_index, dealing_id)| StrippedMessageId::IDkgDealing(dealing_id, node_index));

        ingress_messages.chain(idkg_dealings).collect()
    }
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/download.rs (L301-375)
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
```
