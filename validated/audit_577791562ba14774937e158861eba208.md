### Title
Single Byzantine Subnet Peer Can Repeatedly Abort Ongoing State Sync via `NO_CONTENT` Responses — (`rs/p2p/state_sync_manager/src/ongoing.rs`)

---

### Summary

A single Byzantine subnet node can permanently prevent a victim replica from completing state sync by repeatedly returning `StatusCode::NO_CONTENT` for every chunk request. Each `NO_CONTENT` response causes the peer to be evicted from `active_downloads`; when it is the only peer, the `active_downloads.is_empty()` guard breaks the sync loop, drops the `Chunkable` tracker (losing all progress), and stalls the node until the next advert broadcast cycle (~5 s). Because the Byzantine peer re-advertises every broadcast interval and can again be the first (or only) peer added to the fresh sync, it can repeat this indefinitely.

---

### Finding Description

**Step 1 — `NO_CONTENT` is mapped to a permanent peer-eviction error.**

In `routes/chunk.rs`, the server-side handler returns `StatusCode::NO_CONTENT` when `state_sync.chunk()` returns `None`. [1](#0-0) 

The client-side parser maps that status code to `DownloadChunkError::NoContent`: [2](#0-1) 

**Step 2 — `NoContent` unconditionally evicts the peer.**

`handle_downloaded_chunk_result` treats `NoContent` as a *permanent* error and removes the peer from `active_downloads`, decrementing `allowed_downloads` by `PARALLEL_CHUNK_DOWNLOADS`: [3](#0-2) 

Unlike `Overloaded`, `Timeout`, and `Cancelled` — which only re-queue the chunk without touching `active_downloads` — `NoContent` and `RequestError` both evict the peer permanently for the lifetime of that sync instance. [4](#0-3) 

**Step 3 — Empty `active_downloads` breaks the run loop and drops the tracker.**

After every download result, the run loop checks:

```rust
if self.active_downloads.is_empty() {
    info!(self.log, "Stopping ongoing state sync because no peers.",);
    break;
}
``` [5](#0-4) 

When the loop breaks, the `Chunkable` tracker (holding all downloaded-chunk progress) is dropped. The `OngoingStateSyncHandle.shutdown` future completes.

**Step 4 — Recovery requires a new advert; Byzantine peer re-enters immediately.**

In `handle_advert`, the manager only detects that the sync has ended when the *next* advert arrives and `ongoing.shutdown.completed()` is true: [6](#0-5) 

At that point `ongoing_state_sync` is set to `None` and `maybe_start_state_sync` is called, creating a brand-new tracker from scratch. The advert broadcast interval is 5 seconds: [7](#0-6) 

Because the Byzantine peer also re-advertises every 5 seconds, it can again be the first (or only) peer added to the new sync instance, and the cycle repeats indefinitely.

**Step 5 — The attack window is real.**

When the Byzantine peer's advert arrives first, the sync starts with it as the sole entry in `active_downloads`. The Byzantine peer can return `NO_CONTENT` in milliseconds (no delay required), breaking the sync before any honest peer's advert arrives. Honest peers' adverts are only added via `try_send` while the sync is *still running*: [8](#0-7) 

Once the sync has already broken, those in-flight `try_send` calls go to a closed channel and are silently dropped.

---

### Impact Explanation

- The victim replica can never complete state sync as long as the Byzantine peer keeps disrupting it.
- Every restart drops the `Chunkable` tracker, losing all previously downloaded chunks and restarting from zero.
- A replica that cannot complete state sync falls behind consensus, cannot execute blocks, and — if it is a subnet member — degrades subnet liveness.

---

### Likelihood Explanation

- Requires the attacker to be a registered subnet node (NNS-approved), which is a meaningful barrier but is explicitly within the stated scope ("protocol peer behavior below the consensus fault threshold").
- The attack is most effective when the Byzantine peer is the only node advertising the target state, or when it can reliably win the 5-second advert race. Both conditions are achievable in practice (e.g., a lagging subnet where only one or two nodes have the target checkpoint).
- No cryptographic material needs to be compromised; the attacker simply returns a valid HTTP `204 No Content` response.

---

### Recommendation

1. **Distinguish transient from permanent `NoContent`.** A peer that returns `NO_CONTENT` for a single chunk should not be permanently evicted. Instead, re-queue the chunk (as is done for `Overloaded`/`Timeout`) and only evict after a configurable threshold of consecutive `NoContent` responses from the same peer.

2. **Do not break the run loop when `active_downloads` is empty if there are still in-flight downloads or pending peers.** The loop could instead wait for the next `new_peers_rx` message before giving up.

3. **Preserve the `Chunkable` tracker across peer-set changes.** The tracker should only be dropped on successful completion or explicit cancellation, not on transient peer exhaustion.

---

### Proof of Concept

State-machine test (pseudocode):

```
1. Start StateSyncManager with one Byzantine peer (NODE_BYZ).
2. NODE_BYZ sends a valid advert for artifact_id H1.
3. handle_advert starts a new OngoingStateSync; NODE_BYZ is the only entry in active_downloads.
4. NODE_BYZ's transport mock returns StatusCode::NO_CONTENT for every /state-sync/chunk request.
5. After the first chunk download completes:
   - handle_downloaded_chunk_result removes NODE_BYZ from active_downloads.
   - active_downloads.is_empty() == true → run loop breaks.
   - Chunkable tracker is dropped (zero chunks retained).
6. Assert: ongoing.shutdown.completed() == true within one CHUNK_DOWNLOAD_TIMEOUT (10 s).
7. Introduce an honest peer (NODE_HON) that re-advertises H1 after 5 s.
8. handle_advert detects shutdown.completed(), clears ongoing_state_sync, calls maybe_start_state_sync → new sync starts from scratch.
9. NODE_BYZ re-advertises H1 and is added to the new sync before NODE_HON's chunks arrive.
10. Repeat steps 4–9 N times; assert that the sync never completes despite NODE_HON being available.
```

This is directly testable with the existing `MockTransport` / `MockChunkable` infrastructure already present in the test suite. [9](#0-8)

### Citations

**File:** rs/p2p/state_sync_manager/src/routes/chunk.rs (L68-68)
```rust
                None => Err(StatusCode::NO_CONTENT),
```

**File:** rs/p2p/state_sync_manager/src/routes/chunk.rs (L128-128)
```rust
        StatusCode::NO_CONTENT => Err(DownloadChunkError::NoContent),
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

**File:** rs/p2p/state_sync_manager/src/ongoing.rs (L211-222)
```rust
            Err(
                err @ (DownloadChunkError::Overloaded
                | DownloadChunkError::Timeout
                | DownloadChunkError::Cancelled),
            ) => {
                info!(
                    every_n_seconds => 15,
                    self.log,
                    "Failed to download chunk from {}: {} ", peer_id, err
                );
                self.chunks_to_download.download_failed(chunk_id);
            }
```

**File:** rs/p2p/state_sync_manager/src/ongoing.rs (L403-462)
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

**File:** rs/p2p/state_sync_manager/src/lib.rs (L46-46)
```rust
const ADVERT_BROADCAST_INTERVAL: Duration = Duration::from_secs(5);
```

**File:** rs/p2p/state_sync_manager/src/lib.rs (L150-154)
```rust
            if ongoing.artifact_id == advert.id {
                // `try_send` is used beacuse the ongoing state sync can be blocked. This can, for example happen because of
                // file system operations. In that case we don't want to block the main event loop here. It is also fine
                // to drop adverts since peers will readvertise anyway.
                let _ = ongoing.sender.try_send(peer_id);
```

**File:** rs/p2p/state_sync_manager/src/lib.rs (L156-164)
```rust
            if ongoing.shutdown.completed() {
                info!(self.log, "Cleaning up state sync {}", advert.id.height);
                self.ongoing_state_sync = None;
            } else {
                if self.state_sync.cancel_if_running(&ongoing.artifact_id) {
                    ongoing.shutdown.cancel();
                }
                return;
            }
```
