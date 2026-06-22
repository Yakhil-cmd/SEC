### Title
Missing `ingress_bytes_hash` Length Validation Enables Byzantine Peer to Stall Block Assembly — (`rs/p2p/artifact_downloader/src/fetch_stripped_artifact/types.rs`)

### Summary

`TryFrom<pb::StrippedIngressMessage> for SignedIngressId` wraps the raw protobuf bytes field directly into a `CryptoHashOf` without validating that it is exactly 32 bytes. A Byzantine subnet peer can send a `StrippedBlockProposal` whose `ingress_bytes_hash` fields are of arbitrary length. Because every local and peer-side comparison derives a real 32-byte SHA-256 hash, the malformed ID never matches, causing the assembler to loop indefinitely on peer-download retries for every ingress in the block until the bouncer eventually marks the proposal unwanted.

### Finding Description

In `TryFrom<pb::StrippedIngressMessage> for SignedIngressId`:

```rust
// types.rs line 47 — no length check
let ingress_bytes_hash = CryptoHashOf::from(CryptoHash(value.ingress_bytes_hash));
```

`CryptoHash` is a plain `struct CryptoHash(pub Vec<u8>)` newtype; `CryptoHashOf::from` is a zero-cost wrapper. Neither enforces a 32-byte invariant. Any `Vec<u8>` of any length is accepted and stored verbatim. [1](#0-0) 

The legitimate path always produces a 32-byte hash:

```rust
// types.rs line 30
ingress_bytes_hash: ic_types::crypto::crypto_hash(bytes),
``` [2](#0-1) 

**Local lookup (sender side — `get_ingress`):** The serving node compares `SignedIngressId::from(&ingress_message) == *signed_ingress_id`. The left side is always 32 bytes; the right side carries the attacker-controlled length. The guard at line 101 and 132 always fails. [3](#0-2) [4](#0-3) 

**Peer-download validation (`parse_response`):** After fetching the actual ingress bytes from a peer, the receiver recomputes `derived_ingress_id = SignedIngressId::from(&ingress)` (32-byte hash) and checks `derived_ingress_id == *ingress_id` (malformed hash). This always returns `ParseResponseError::MessageIdMismatch`, so the download loop never terminates. [5](#0-4) 

**Assembler loop:** `assemble_message` spawns one `get_or_fetch` task per stripped message ID and waits for all of them to complete. With malformed IDs, none ever complete. The only exit is the bouncer becoming `Unwanted` (refresh period: 3 s, checked in a `tokio::select!`). [6](#0-5) 

The `download_stripped_message` retry loop uses exponential backoff from 5 s up to 120 s with no maximum elapsed time (`with_max_elapsed_time(None)`), so it generates sustained outbound RPC traffic for the entire window the block is considered wanted. [7](#0-6) 

### Impact Explanation

- Every ingress in the targeted block proposal bypasses the local-pool fast path and enters the peer-download loop.
- The download loop retries indefinitely (up to 120 s between attempts) against random peers, amplifying outbound RPC traffic proportionally to the number of ingresses in the block.
- Block assembly for the targeted proposal is stalled on the victim node until the bouncer fires (≥3 s refresh cycle), delaying the node's ability to validate and vote on that proposal.
- A single Byzantine node can target every block proposal it receives, continuously stalling assembly on victim nodes and degrading consensus throughput.

### Likelihood Explanation

The attacker needs only to be a subnet node (below the consensus fault threshold). No key material, governance majority, or external infrastructure is required. The malformed field is a plain protobuf `bytes` field with no schema-level length constraint, so crafting the payload is trivial. The attack is repeatable on every block proposal.

### Recommendation

Add an explicit 32-byte length check in `TryFrom<pb::StrippedIngressMessage> for SignedIngressId` before constructing the `CryptoHashOf`:

```rust
const CRYPTO_HASH_LEN: usize = 32;
if value.ingress_bytes_hash.len() != CRYPTO_HASH_LEN {
    return Err(ProxyDecodeError::Other(format!(
        "ingress_bytes_hash must be {} bytes, got {}",
        CRYPTO_HASH_LEN,
        value.ingress_bytes_hash.len()
    )));
}
let ingress_bytes_hash = CryptoHashOf::from(CryptoHash(value.ingress_bytes_hash));
```

Apply the same guard to `GetIngressMessageInBlockRequest::try_from` in `rpc.rs` (line 34), which has the identical pattern. [8](#0-7) 

### Proof of Concept

```rust
#[test]
fn malformed_ingress_bytes_hash_never_matches_local() {
    use ic_protobuf::types::v1 as pb;
    use crate::fetch_stripped_artifact::types::SignedIngressId;

    // Build a real ingress and its legitimate ID
    let ingress = SignedIngressBuilder::new().nonce(1).build();
    let real_id = SignedIngressId::from(&ingress);

    // Craft StrippedIngressMessages with wrong-length hashes
    for bad_len in [0usize, 1, 31, 33, 1024] {
        let proto = pb::StrippedIngressMessage {
            stripped: Some(real_id.ingress_message_id.clone().into()),
            ingress_bytes_hash: vec![0xAB; bad_len],
        };
        // Currently succeeds — should return Err after the fix
        let parsed = SignedIngressId::try_from(proto).unwrap();
        // The parsed ID never equals the legitimately-computed one
        assert_ne!(parsed, real_id,
            "bad_len={bad_len}: malformed hash should not match real hash");
    }
}
```

### Citations

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/types.rs (L27-32)
```rust
    pub(crate) fn new(ingress_message_id: IngressMessageId, bytes: &SignedRequestBytes) -> Self {
        Self {
            ingress_message_id,
            ingress_bytes_hash: ic_types::crypto::crypto_hash(bytes),
        }
    }
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/types.rs (L41-54)
```rust
impl TryFrom<pb::StrippedIngressMessage> for SignedIngressId {
    type Error = ProxyDecodeError;

    fn try_from(value: pb::StrippedIngressMessage) -> Result<Self, Self::Error> {
        let ingress_message_id =
            try_from_option_field(value.stripped, "StrippedIngressMessage::stripped")?;
        let ingress_bytes_hash = CryptoHashOf::from(CryptoHash(value.ingress_bytes_hash));

        Ok(SignedIngressId {
            ingress_message_id,
            ingress_bytes_hash,
        })
    }
}
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/download.rs (L98-105)
```rust
        if let Some(ingress_message) = self.ingress_pool.read().unwrap().get(ingress_message_id) {
            // Make sure that this is the correct ingress message. [`IngressMessageId`] does _not_
            // uniquely identify ingress messages, we thus need to perform an extra check.
            if SignedIngressId::from(&ingress_message) == *signed_ingress_id {
                self.metrics.report_stripped_message_in_pool(message_type);
                return Ok(ingress_message.into());
            }
        }
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/download.rs (L128-141)
```rust
            Some(bytes)
            // Make sure that this is the correct ingress message. [`IngressMessageId`]
            // does _not_ uniquely identify ingress messages, we thus need to perform
            // an extra check.
                if SignedIngressId::new(ingress_message_id.clone(), bytes)
                    == *signed_ingress_id =>
            {
                self.metrics.report_stripped_message_in_block(message_type);
                Ok(bytes.clone())
            }
            _ => {
                self.metrics.report_stripped_message_not_found(message_type);
                Err(IngressPoolAccessError::IngressMessageNotFound)
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

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/download.rs (L383-408)
```rust
fn parse_response(
    message_id: &StrippedMessageId,
    body: Bytes,
) -> Result<StrippedMessage, ParseResponseError> {
    match &message_id {
        StrippedMessageId::Ingress(ingress_id) => {
            let ingress = parse_ingress_response(body)?;
            let derived_ingress_id = SignedIngressId::from(&ingress);
            if derived_ingress_id == *ingress_id {
                return Ok(StrippedMessage::Ingress(derived_ingress_id, ingress));
            }
        }
        StrippedMessageId::IDkgDealing(dealing_id, node_index) => {
            let dealing = parse_dealing_response(body)?;
            let derived_dealing_id = dealing.message_id();
            if derived_dealing_id == *dealing_id {
                return Ok(StrippedMessage::IDkgDealing(
                    derived_dealing_id,
                    *node_index,
                    dealing,
                ));
            }
        }
    }
    Err(ParseResponseError::MessageIdMismatch)
}
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L211-242)
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

        loop {
            let join_result = tokio::select! {
                _ = bouncer.wait_for(|bouncer| matches!(bouncer(&id), BouncerValue::Unwanted)) => {
                    self.metrics.report_aborted_block_assembly();
                    return AssembleResult::Unwanted;
                }
                join_result = join_set.join_next() => join_result,
            };
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/types/rpc.rs (L34-34)
```rust
        let ingress_bytes_hash = CryptoHashOf::from(CryptoHash(value.ingress_bytes_hash));
```
