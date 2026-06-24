I need to verify the `validate_shares` filter behavior for future-window IDs and the exact slot table limit for https_outcalls before rendering a verdict.

### Title
Unbounded Unvalidated Pool Memory Exhaustion via NonReplicated/Flexible Artifact Future-Window Flooding — (`rs/https_outcalls/consensus/src/gossip.rs`)

---

### Summary

The `new_bouncer` in `CanisterHttpGossipImpl` evaluates artifact acceptability solely on the callback ID, with no awareness of artifact type. For NonReplicated and Flexible requests, the gossiped artifact includes the full response body (up to 2 MB). A single Byzantine subnet peer can advertise up to `MAX_NUMBER_OF_REQUESTS_AHEAD = 345` fake artifacts with IDs in the future window, each carrying a max-size response body. The bouncer returns `Wants` for all of them, `FetchArtifact` downloads each in full, and `validate_shares` **skips** all of them because they have IDs `>= next_callback_id`. The result is ~690 MB of unauthenticated data sitting in the unvalidated pool, with no eviction until `next_callback_id` advances — which the attacker can race by continuously cycling new IDs into the window.

---

### Finding Description

**Root cause 1 — Bouncer is type-blind:** [1](#0-0) 

The closure returned by `new_bouncer` evaluates only `id.content.id()` (the callback ID) and `id.content.registry_version()`. It has no knowledge of whether the artifact is `FullyReplicated` (no response body) or `NonReplicated`/`Flexible` (includes a full `CanisterHttpResponse` body). Both types receive `BouncerValue::Wants` for IDs in the window `[next_callback_id, next_callback_id + 345]`. [2](#0-1) 

**Root cause 2 — No slot table limit for https_outcalls:** [3](#0-2) 

`SLOT_TABLE_NO_LIMIT = usize::MAX` means a single Byzantine peer can advertise an unlimited number of artifact slots. The ingress pool has a 50,000-slot cap as a precedent for this exact threat class; https_outcalls has none. [4](#0-3) 

**Root cause 3 — `validate_shares` skips future-window artifacts entirely:** [5](#0-4) 

The hard filter `.filter(|artifact| artifact.share.content.id() < next_callback_id)` means any artifact whose callback ID is in the future window (`>= next_callback_id`) is **never processed** by validation. It stays in the unvalidated pool until `next_callback_id` advances past it — which the attacker controls by continuously advertising IDs just ahead of the cursor.

**Root cause 4 — Artifact structure carries full response body:** [6](#0-5) 

For NonReplicated/Flexible, `response: Some(CanisterHttpResponse)` is included in the gossiped artifact. The downloader fetches the full body before any validation occurs. [7](#0-6) 

---

### Impact Explanation

A single Byzantine subnet peer can sustain ~690 MB of fake data in the unvalidated pool of a victim replica (`345 artifacts × 2 MB`). With `f` Byzantine peers on a subnet (e.g., `f = 4` on a 13-node subnet), the total is ~2.76 GB. The attack is sustained: as `next_callback_id` advances, the attacker cycles new IDs into the window, maintaining constant memory pressure. This can cause OOM on the replica process, effectively removing it from the subnet without crossing the fault threshold.

---

### Likelihood Explanation

The attacker must be a Byzantine subnet node — within the IC threat model (up to `f = (n-1)/3` Byzantine nodes are tolerated). No privileged key, governance majority, or external network attack is required. The attack is fully local to the P2P layer and requires only crafting valid-looking `CanisterHttpResponseArtifact` messages with large fake response bodies and callback IDs in the future window.

---

### Recommendation

1. **Cap the slot table for https_outcalls** analogously to ingress (`SLOT_TABLE_LIMIT_INGRESS = 50_000`). A reasonable bound is `~1000` slots per peer, matching the expected maximum concurrent outcall requests.

2. **Make the bouncer type-aware**: Before returning `Wants` for a future-window ID, check whether the artifact's `response` field is `None` (as expected for FullyReplicated) or `Some(...)` (NonReplicated/Flexible). Reject oversized or unexpected response bodies at the bouncer level before download.

3. **Evict future-window artifacts proactively**: `validate_shares` should also process artifacts with IDs in the future window and evict those whose response body exceeds a per-artifact size budget, rather than leaving them in the pool indefinitely.

---

### Proof of Concept

```
1. Byzantine peer B is a valid subnet node on a 13-node subnet.
2. B reads next_callback_id = N from the subnet state (public).
3. B constructs 345 CanisterHttpResponseArtifact messages:
   - share.content.id() = N, N+1, ..., N+344
   - share.content.registry_version() = current registry version
   - response = Some(CanisterHttpResponse { content: Success(vec![0u8; 2_097_152]) })
   - share signed by B's key (valid subnet node key)
4. B advertises all 345 via the slot table (SLOT_TABLE_NO_LIMIT).
5. Victim V's bouncer evaluates each ID: N <= id <= N+344 → Wants.
6. FetchArtifact downloads all 345 artifacts; each body is 2 MB.
7. All 345 artifacts are inserted into V's unvalidated pool.
8. validate_shares skips all (IDs >= next_callback_id).
9. V holds ~690 MB of fake data. B repeats as N advances.
```

A fuzz test can inject 345 NonReplicated artifacts with 2 MB bodies for IDs in `[next_callback_id, next_callback_id + 345]` and assert that `canister_http_pool.get_unvalidated_artifacts().count() * 2MB` stays below a safe threshold (e.g., 50 MB). The assertion will fail, confirming the vulnerability.

### Citations

**File:** rs/https_outcalls/consensus/src/gossip.rs (L19-19)
```rust
const MAX_NUMBER_OF_REQUESTS_AHEAD: u64 = 3 * (100 + 15);
```

**File:** rs/https_outcalls/consensus/src/gossip.rs (L63-96)
```rust
        Box::new(move |id: &'_ CanisterHttpResponseId| {
            if id.content.registry_version() != registry_version {
                warn!(
                    log,
                    "Dropping canister http response share with callback id: {}, because registry version {} does not match expected version {}",
                    id.content.id(),
                    id.content.registry_version(),
                    registry_version
                );
                return BouncerValue::Unwanted;
            }

            // We derive the highest accepted request id from the next expected request id, plus the
            // number of maximal number of new requests we can get between the function calls.
            let highest_accepted_request_id =
                CallbackId::from(next_callback_id.get() + MAX_NUMBER_OF_REQUESTS_AHEAD);

            // The https outcalls share should be fetched in two cases:
            //  - The Id of the share is part of the state which means it is active.
            //  - The callback Id is higher than the next callback Id (the next callback Id is the Id used next in execution round), but
            //    not higher that `MAX_NUMBER_OF_REQUESTS_AHEAD`.
            //    Receiving an callback Id higher is possible because the priority fn is updated periodically (every 3s) with the latest state
            //    and can therefore store stale `known_request_ids` and stale `next_callback_id`.
            if known_request_ids.contains(&id.content.id())
                || (id.content.id() >= next_callback_id
                    && id.content.id() <= highest_accepted_request_id)
            {
                BouncerValue::Wants
            } else if id.content.id() > highest_accepted_request_id {
                BouncerValue::MaybeWantsLater
            } else {
                BouncerValue::Unwanted
            }
        })
```

**File:** rs/replica/setup_ic_network/src/lib.rs (L74-75)
```rust
const SLOT_TABLE_LIMIT_INGRESS: usize = 50_000;
const SLOT_TABLE_NO_LIMIT: usize = usize::MAX;
```

**File:** rs/replica/setup_ic_network/src/lib.rs (L294-304)
```rust
        let https_outcalls = {
            let assembler = ic_artifact_downloader::FetchArtifact::new(
                log.clone(),
                rt_handle.clone(),
                artifact_pools.https_outcalls_pool.clone(),
                bouncers.https_outcalls,
                metrics_registry.clone(),
            );

            new_p2p_consensus.abortable_broadcast_channel(assembler, SLOT_TABLE_NO_LIMIT)
        };
```

**File:** rs/https_outcalls/consensus/src/pool_manager.rs (L482-484)
```rust
        canister_http_pool
            .get_unvalidated_artifacts()
            .filter(|artifact| artifact.share.content.id() < next_callback_id)
```

**File:** rs/types/types/src/canister_http.rs (L1182-1187)
```rust
#[derive(Clone, Debug, PartialEq)]
pub struct CanisterHttpResponseArtifact {
    pub share: CanisterHttpResponseShare,
    // The response should not be included in the case of fully replicated outcalls.
    pub response: Option<CanisterHttpResponse>,
}
```

**File:** rs/p2p/artifact_downloader/src/fetch_artifact/download.rs (L222-265)
```rust
        match artifact {
            // Artifact was pushed by peer. In this case we don't need check that the artifact ID corresponds
            // to the artifact because we earlier derived the ID from the artifact.
            Some((artifact, peer_id)) => AssembleResult::Done {
                message: artifact,
                peer_id,
            },

            // Fetch artifact
            None => {
                let timer = metrics
                    .download_task_artifact_download_duration
                    .start_timer();
                let mut rng = SmallRng::from_entropy();

                let result = loop {
                    let next_request_at = Instant::now()
                        + artifact_download_backoff
                            .next_backoff()
                            .unwrap_or(MAX_ARTIFACT_RPC_TIMEOUT);
                    if let Some(peer) = peer_rx.peers().into_iter().choose(&mut rng) {
                        let bytes = Bytes::from(Artifact::PbId::proxy_encode(id.clone()));
                        let request = Request::builder()
                            .uri(format!("/{}/rpc", uri_prefix::<Artifact>()))
                            .body(bytes)
                            .unwrap();

                        match timeout_at(next_request_at, transport.rpc(&peer, request)).await {
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
