Looking at the code carefully across all relevant files to trace the exact behavior.

### Title
Byzantine Peer Permanent State Sync Stall via Persistent `Overloaded`/`Timeout` Responses — (`rs/p2p/state_sync_manager/src/ongoing.rs`)

---

### Summary

The `handle_downloaded_chunk_result` function in `OngoingStateSync` applies asymmetric peer-removal logic: `NoContent` and `RequestError` both evict the peer from `active_downloads`, but `Overloaded`, `Timeout`, and `Cancelled` only re-queue the chunk via `download_failed`. A Byzantine peer that is the sole advertiser of a CUP-referenced state can exploit this by always returning `StatusCode::TOO_MANY_REQUESTS` or `StatusCode::REQUEST_TIMEOUT`, keeping itself permanently in `active_downloads` while the victim loops indefinitely without making progress.

---

### Finding Description

**Asymmetric error handling — confirmed in code:**

In `parse_chunk_handler_response`, the HTTP status codes are mapped as follows: [1](#0-0) 

`TOO_MANY_REQUESTS` → `Overloaded`, `REQUEST_TIMEOUT` → `Timeout`.

In `handle_downloaded_chunk_result`, the match arms diverge critically:

- `NoContent` and `RequestError` both call `active_downloads.remove(&peer_id)` and decrement `allowed_downloads`: [2](#0-1) 

- `Overloaded | Timeout | Cancelled` only calls `chunks_to_download.download_failed(chunk_id)` — the peer is **never removed**: [3](#0-2) 

`download_failed` simply re-pushes the chunk back onto the queue: [4](#0-3) 

**Loop termination — only on empty `active_downloads`:**

The `run` loop exits only when `active_downloads` is empty or the cancellation token fires: [5](#0-4) 

If the Byzantine peer is the only entry in `active_downloads` and always returns 429, it is never removed, so the loop never exits on its own.

**No per-peer failure counter, no backoff, no eviction threshold** exist anywhere in `OngoingStateSync` for `Overloaded`/`Timeout` errors.

**The outer restart mechanism does not break the cycle:**

`StateSyncManager::handle_advert` calls `cancel_if_running` when a new advert arrives: [6](#0-5) 

Even if this eventually cancels the sync (the code comment acknowledges a 5–10 min restart cycle), the Byzantine peer immediately re-advertises the same state, causing `maybe_start_state_sync` to return a new `Chunkable` and restart the sync — again with the same Byzantine peer as the only advertiser. The cycle repeats indefinitely.

**The existing test confirms the gap:** `test_cancel_if_running` uses a `MockTransport` that always returns `TOO_MANY_REQUESTS` but only asserts that `shutdown()` works — it does not assert that the peer is eventually evicted or that progress is made: [7](#0-6) 

---

### Impact Explanation

A victim replica that is behind consensus height and needs state sync to rejoin is permanently excluded from subnet participation. If multiple replicas are targeted simultaneously, the effective fault tolerance of the subnet drops below the safety threshold, threatening liveness and potentially safety.

---

### Likelihood Explanation

The precondition — Byzantine peer being the sole advertiser — is realistic in two scenarios:

1. The victim is far behind; honest peers have garbage-collected the old state and return `NoContent` (which removes them from `active_downloads`), leaving only the Byzantine peer.
2. The Byzantine peer is one of a small set of nodes that still holds the CUP-referenced state, and it can outlast honest peers by never serving chunks.

The attacker is an unprivileged subnet peer operating below the Byzantine fault threshold (a single node suffices). No admin keys, governance majority, or DDoS is required — only the ability to control one peer's HTTP responses.

---

### Recommendation

Add a per-peer consecutive-failure counter for `Overloaded` and `Timeout` errors. After a configurable threshold (e.g., `PARALLEL_CHUNK_DOWNLOADS` consecutive failures), treat the peer identically to `RequestError` — remove it from `active_downloads` and decrement `allowed_downloads`. `Cancelled` should remain exempt since it is an internal signal, not a peer-controlled response.

---

### Proof of Concept

```
1. Byzantine peer B sends a valid advert for CUP-referenced state S to victim V.
2. V calls maybe_start_state_sync → returns Chunkable; B is added to active_downloads.
3. V dispatches up to PARALLEL_CHUNK_DOWNLOADS=10 chunk requests to B.
4. B responds to every request with HTTP 429 (TOO_MANY_REQUESTS).
5. parse_chunk_handler_response maps 429 → DownloadChunkError::Overloaded.
6. handle_downloaded_chunk_result matches Overloaded arm:
   - calls chunks_to_download.download_failed(chunk_id)  [chunk re-queued]
   - does NOT call active_downloads.remove(&peer_id)
7. spawn_chunk_downloads sees allowed_downloads=10, active_downloads={B:0},
   dispatches the same chunks again to B.
8. Steps 4–7 repeat indefinitely; V never completes state sync.
9. After 5–10 min, cancel_if_running fires; V restarts state sync.
10. B re-advertises S; V starts a new sync with B as sole peer → goto step 3.
11. V never rejoins consensus.
```

Reproducible with `MockTransport::expect_rpc().returning(|_,_| Ok(Response::builder().status(StatusCode::TOO_MANY_REQUESTS)...))` and asserting that after N iterations `active_downloads` still contains B and no chunks have been successfully added — contrasted with `NO_CONTENT` which removes B after the first failure.

### Citations

**File:** rs/p2p/state_sync_manager/src/routes/chunk.rs (L128-131)
```rust
        StatusCode::NO_CONTENT => Err(DownloadChunkError::NoContent),
        StatusCode::TOO_MANY_REQUESTS => Err(DownloadChunkError::Overloaded),
        StatusCode::REQUEST_TIMEOUT => Err(DownloadChunkError::Timeout),
        _ => Err(DownloadChunkError::RequestError {
```

**File:** rs/p2p/state_sync_manager/src/ongoing.rs (L173-176)
```rust
            if self.active_downloads.is_empty() {
                info!(self.log, "Stopping ongoing state sync because no peers.",);
                break;
            }
```

**File:** rs/p2p/state_sync_manager/src/ongoing.rs (L194-209)
```rust
            Err(DownloadChunkError::NoContent) => {
                if self.active_downloads.remove(&peer_id).is_some() {
                    self.allowed_downloads -= PARALLEL_CHUNK_DOWNLOADS;
                }

                self.chunks_to_download.download_failed(chunk_id);
            }
            Err(DownloadChunkError::RequestError { chunk_id, err }) => {
                info!(
                    self.log,
                    "Failed to download chunk {} from {}: {} ", chunk_id, peer_id, err
                );
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

**File:** rs/p2p/state_sync_manager/src/ongoing.rs (L429-461)
```rust
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

**File:** rs/p2p/state_sync_manager/src/ongoing/chunks_to_download.rs (L26-28)
```rust
    pub(crate) fn download_failed(&mut self, chunk_id: ChunkId) {
        self.chunks.push(chunk_id);
    }
```

**File:** rs/p2p/state_sync_manager/src/lib.rs (L160-163)
```rust
                if self.state_sync.cancel_if_running(&ongoing.artifact_id) {
                    ongoing.shutdown.cancel();
                }
                return;
```
