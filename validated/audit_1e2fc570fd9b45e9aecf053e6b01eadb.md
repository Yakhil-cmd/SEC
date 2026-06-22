Let me trace the exact code paths carefully before rendering a verdict.

The code confirms the full attack path. No rate-limiting, blacklisting, or penalization exists anywhere in the module. Here is the verdict:

---

### Title
Byzantine Peer Repeatedly Cycles State Sync via Unconditional `NoContent` Removal — (`rs/p2p/state_sync_manager/src/ongoing.rs`)

---

### Summary
A single Byzantine subnet peer can permanently stall state sync on a lagging replica by advertising a valid state, immediately returning `NO_CONTENT` for every chunk request (causing its own removal), waiting for the sync to self-terminate, then re-advertising to restart the cycle. No rate-limiting, cooldown, or peer penalization exists anywhere in the state sync manager.

---

### Finding Description

The cycle is mechanically exact across three functions:

**1. `NoContent` unconditionally removes the peer and decrements the download budget** [1](#0-0) 

**2. An empty `active_downloads` map immediately breaks the event loop, completing the `Shutdown` handle** [2](#0-1) 

**3. `handle_advert` detects `shutdown.completed()`, clears `ongoing_state_sync`, then unconditionally calls `maybe_start_state_sync` again for the same peer's next advert** [3](#0-2) 

**4. The `Entry::Vacant` guard that prevents duplicate peer insertion only applies within a single sync instance; it is reset on every new `start_ongoing_state_sync` call** [4](#0-3) 

**5. A grep across the entire module confirms zero rate-limiting, blacklisting, cooldown, or penalization logic**



The exact call sequence:

```
Byzantine peer sends advert
  → handle_advert: ongoing_state_sync is None → maybe_start_state_sync → new OngoingStateSync spawned
  → ongoing.run: peer added to active_downloads, 10 chunk downloads dispatched
  → peer returns NO_CONTENT on first chunk
  → handle_downloaded_chunk_result: active_downloads.remove(&peer_id), allowed_downloads -= 10
  → active_downloads.is_empty() == true → break (sync terminates, Shutdown completes)
  → Byzantine peer re-sends advert
  → handle_advert: ongoing.shutdown.completed() == true → ongoing_state_sync = None
  → maybe_start_state_sync called again → cycle repeats
```

---

### Impact Explanation

When the Byzantine peer is the sole advertiser of the target state (e.g., a new node joining a subnet where only one peer has the checkpoint), the lagging replica is permanently unable to complete state sync. It cannot participate in consensus, effectively removing it from the subnet. Even when honest peers co-advertise, the Byzantine peer causes repeated sync restarts, wasting CPU in the advert handler and the `OngoingStateSync` event loop, and delaying completion proportionally to the fraction of chunk downloads assigned to the Byzantine peer.

---

### Likelihood Explanation

The attacker must be a legitimate subnet node (below the Byzantine fault threshold). No special privileges are required beyond being a peer in the transport layer. The attack requires only: (a) sending a valid `StateSyncArtifactId` advert (correct height + hash, which is public information from the consensus chain), and (b) responding to all chunk RPCs with HTTP `204 NO_CONTENT`. Both are trivially achievable by a compromised node. The advert channel has a capacity of 20: [5](#0-4) 

but this only throttles the queue, not the cycle rate. The cycle speed is bounded by the `CHUNK_DOWNLOAD_TIMEOUT` of 10 seconds per round: [6](#0-5) 

meaning the Byzantine peer can force ~6 restart cycles per minute indefinitely.

---

### Recommendation

1. **Peer penalization**: Track per-peer `NoContent` counts across sync instances (e.g., in a `HashMap<NodeId, (u32, Instant)>` in `StateSyncManager`). After N consecutive `NoContent` failures, refuse to add that peer for a configurable backoff window.
2. **Sync restart cooldown**: Record the last time `maybe_start_state_sync` was called for a given `StateSyncArtifactId`. Enforce a minimum interval (e.g., 30 seconds) before restarting a sync for the same height.
3. **Minimum peer requirement**: Before terminating an ongoing sync due to empty `active_downloads`, check whether the peer was removed due to `NoContent` vs. a legitimate disconnect, and log a warning that can feed into reputation scoring.

---

### Proof of Concept

Integration test sketch (production mock layer, no external dependencies):

```rust
// Mock peer that always returns NO_CONTENT
t.expect_rpc().returning(|_, _| {
    Ok(Response::builder()
        .status(StatusCode::NO_CONTENT)
        .body(Bytes::new())
        .unwrap())
});

// Send advert N times from Byzantine peer
for _ in 0..10 {
    handler_tx.send((advert.clone(), BYZANTINE_NODE)).await.unwrap();
    tokio::time::sleep(Duration::from_millis(100)).await;
}

// Assert: state sync never completed AND Byzantine peer was not rate-limited
// (metrics.state_syncs_total == 10, state never advanced)
```

This matches the existing test infrastructure already present in `ongoing.rs` tests. [7](#0-6)

### Citations

**File:** rs/p2p/state_sync_manager/src/ongoing.rs (L46-46)
```rust
const CHUNK_DOWNLOAD_TIMEOUT: Duration = Duration::from_secs(10);
```

**File:** rs/p2p/state_sync_manager/src/ongoing.rs (L147-158)
```rust
                Some(new_peer) = self.new_peers_rx.recv() => {
                    if let Entry::Vacant(e) = self.active_downloads.entry(new_peer) {
                        info!(
                            self.log,
                            "Adding peer {} to ongoing state sync of height {}.",
                            new_peer,
                            self.artifact_id.height
                        );
                        e.insert(0);
                        self.allowed_downloads += PARALLEL_CHUNK_DOWNLOADS;
                        self.spawn_chunk_downloads(cancellation.clone(), tracker.clone());
                    }
```

**File:** rs/p2p/state_sync_manager/src/ongoing.rs (L173-176)
```rust
            if self.active_downloads.is_empty() {
                info!(self.log, "Stopping ongoing state sync because no peers.",);
                break;
            }
```

**File:** rs/p2p/state_sync_manager/src/ongoing.rs (L194-199)
```rust
            Err(DownloadChunkError::NoContent) => {
                if self.active_downloads.remove(&peer_id).is_some() {
                    self.allowed_downloads -= PARALLEL_CHUNK_DOWNLOADS;
                }

                self.chunks_to_download.download_failed(chunk_id);
```

**File:** rs/p2p/state_sync_manager/src/ongoing.rs (L403-461)
```rust
#[cfg(test)]
mod tests {
    use axum::http::{Response, StatusCode};
    use bytes::{Bytes, BytesMut};
    use ic_interfaces::p2p::state_sync::AddChunkError;
    use ic_metrics::MetricsRegistry;
    use ic_p2p_test_utils::mocks::{MockChunkable, MockTransport};
    use ic_test_utilities_logger::with_test_replica_logger;
    use ic_types::{Height, crypto::CryptoHash};
    use ic_types_test_utils::ids::NODE_1;
    use prost::Message;
    use tokio::runtime::Runtime;

    use super::*;
    #[derive(Clone)]
    struct TestMessage;

    fn compress_empty_bytes() -> Bytes {
        let mut raw = BytesMut::new();
        Bytes::new()
            .encode(&mut raw)
            .expect("Allocated enough memory");
        Bytes::from(zstd::bulk::compress(&raw, zstd::DEFAULT_COMPRESSION_LEVEL).unwrap())
    }

    /// Verify that state sync gets aborted if state sync should be cancelled.
    #[test]
    fn test_cancel_if_running() {
        with_test_replica_logger(|log| {
            let mut t = MockTransport::default();
            t.expect_rpc().returning(|_, _| {
                Ok(Response::builder()
                    .status(StatusCode::TOO_MANY_REQUESTS)
                    .body(compress_empty_bytes())
                    .unwrap())
            });
            let mut c = MockChunkable::<TestMessage>::default();
            c.expect_chunks_to_download()
                .returning(|| Box::new(std::iter::once(ChunkId::from(1))));

            let rt = Runtime::new().unwrap();
            let ongoing = start_ongoing_state_sync(
                log,
                rt.handle(),
                OngoingStateSyncMetrics::new(&MetricsRegistry::default()),
                Arc::new(Mutex::new(Box::new(c))),
                StateSyncArtifactId {
                    height: Height::from(1),
                    hash: CryptoHash(vec![]),
                },
                Arc::new(t),
            );

            rt.block_on(async move {
                ongoing.sender.send(NODE_1).await.unwrap();
                ongoing.shutdown.shutdown().await.unwrap();
            });
        });
    }
```

**File:** rs/p2p/state_sync_manager/src/lib.rs (L65-65)
```rust
    let (advert_sender, advert_receiver) = tokio::sync::mpsc::channel(20);
```

**File:** rs/p2p/state_sync_manager/src/lib.rs (L149-193)
```rust
        if let Some(ongoing) = &mut self.ongoing_state_sync {
            if ongoing.artifact_id == advert.id {
                // `try_send` is used beacuse the ongoing state sync can be blocked. This can, for example happen because of
                // file system operations. In that case we don't want to block the main event loop here. It is also fine
                // to drop adverts since peers will readvertise anyway.
                let _ = ongoing.sender.try_send(peer_id);
            }
            if ongoing.shutdown.completed() {
                info!(self.log, "Cleaning up state sync {}", advert.id.height);
                self.ongoing_state_sync = None;
            } else {
                if self.state_sync.cancel_if_running(&ongoing.artifact_id) {
                    ongoing.shutdown.cancel();
                }
                return;
            }
        }
        // `maybe_start_state_sync` should not be called if we have ongoing state sync!
        debug_assert!(self.ongoing_state_sync.is_none());
        if let Some(chunkable) = self.state_sync.maybe_start_state_sync(&advert.id) {
            info!(
                self.log,
                "Starting state sync for height {}", advert.id.height
            );
            self.metrics.state_syncs_total.inc();

            // This spawns an event loop that downloads chunks for the specified Id.
            // When the state sync is done or cancelled it will drop the Chunkable object.
            // Until the Chunkable object is dropped 'maybe_start_state_sync' will always return None.
            let ongoing = start_ongoing_state_sync(
                self.log.clone(),
                &self.rt,
                self.metrics.ongoing_state_sync_metrics.clone(),
                Arc::new(Mutex::new(chunkable)),
                advert.id.clone(),
                transport,
            );
            // Add peer that initiated this state sync to ongoing state sync.
            ongoing
                .sender
                .send(peer_id)
                .await
                .expect("Receive side is not dropped");
            self.ongoing_state_sync = Some(ongoing);
        }
```
