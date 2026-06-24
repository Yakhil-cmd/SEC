The code confirms the described behavior precisely. Here is the full analysis:

---

### Title
Byzantine Peer Permanently Stalls State Sync via Persistent HTTP 429 Without Eviction — (`rs/p2p/state_sync_manager/src/ongoing.rs`)

### Summary
A single Byzantine subnet peer that always responds with `HTTP 429 TOO_MANY_REQUESTS` is never removed from the `active_downloads` map. Because `DownloadChunkError::Overloaded` is treated as a transient error with no retry limit, no backoff, and no eviction, the peer occupies all download slots indefinitely and state sync on the victim replica never completes.

### Finding Description

**Step 1 — 429 maps to `Overloaded`.**

In `parse_chunk_handler_response`, HTTP 429 is mapped to `DownloadChunkError::Overloaded`: [1](#0-0) 

**Step 2 — `Overloaded` does NOT evict the peer.**

In `handle_downloaded_chunk_result`, the `Overloaded | Timeout | Cancelled` arm only calls `chunks_to_download.download_failed(chunk_id)`. It never calls `active_downloads.remove(&peer_id)` and never decrements `allowed_downloads`: [2](#0-1) 

Contrast with `NoContent` and `RequestError`, which both remove the peer and decrement the budget: [3](#0-2) 

**Step 3 — The `run` loop never exits.**

The only self-termination condition (besides external cancellation) is `active_downloads.is_empty()`. Since `Overloaded` never removes the peer, this is never true when the Byzantine peer is the only advertiser: [4](#0-3) 

**Step 4 — No higher-level timeout exists.**

`StateSyncManager::run` in `lib.rs` has no periodic timeout for ongoing state sync. The ongoing sync is only cleaned up when `ongoing.shutdown.completed()` (i.e., the inner loop exited) or when `cancel_if_running` returns true (triggered by a new advert for the same artifact). If the Byzantine peer is the only advertiser, neither condition is ever triggered: [5](#0-4) 

**Step 5 — The cycle repeats indefinitely.**

After each 429 response:
1. `active_downloads[peer]` is decremented to 0 (saturating_sub at line 130–132) — peer stays in the map.
2. `download_failed` re-queues the chunk.
3. `spawn_chunk_downloads` selects the peer again (it has 0 active downloads → highest weight).
4. A new download task is spawned for the same chunk against the same peer. [6](#0-5) 

There is no counter of consecutive failures per peer, no exponential backoff, and no circuit breaker anywhere in the codebase.

**Step 6 — The existing test confirms the behavior.**

`test_cancel_if_running` in `ongoing.rs` uses a mock transport that always returns `StatusCode::TOO_MANY_REQUESTS` and only tests that the sync can be externally cancelled — it does not assert that the peer is removed. This confirms the design intent was "transient, retry forever," with no eviction path: [7](#0-6) 

### Impact Explanation
A lagging replica performing state sync is permanently stalled as long as the Byzantine peer is the only (or dominant) advertiser of the target state. The replica cannot rejoin consensus, reducing the effective fault tolerance of the subnet. The attack requires only a single below-threshold Byzantine node and is repeatable across every state sync attempt.

### Likelihood Explanation
Any node operator running a malicious replica can implement this by returning HTTP 429 for all chunk requests. No threshold corruption, key compromise, or privileged access is required. The attacker only needs to be the peer that first advertises the target state to the victim replica.

### Recommendation
In the `Overloaded` arm of `handle_downloaded_chunk_result`, track a per-peer consecutive-overload counter. After a configurable threshold (e.g., `N` consecutive `Overloaded` responses), treat the peer identically to `RequestError` — remove it from `active_downloads` and decrement `allowed_downloads`. Alternatively, apply an exponential backoff that effectively starves the peer of download slots over time.

### Proof of Concept
State-machine test: construct an `OngoingStateSync` with a single mock peer whose transport always returns `StatusCode::TOO_MANY_REQUESTS`. Assert that after `N * PARALLEL_CHUNK_DOWNLOADS` download cycles, either the peer is absent from `active_downloads` or the sync has terminated. Under the current code, neither assertion holds — the loop runs indefinitely until the external cancellation token is triggered.

### Citations

**File:** rs/p2p/state_sync_manager/src/routes/chunk.rs (L129-129)
```rust
        StatusCode::TOO_MANY_REQUESTS => Err(DownloadChunkError::Overloaded),
```

**File:** rs/p2p/state_sync_manager/src/ongoing.rs (L130-134)
```rust
                            self.active_downloads
                                .entry(result.peer_id)
                                .and_modify(|v| *v = v.saturating_sub(1));
                            self.handle_downloaded_chunk_result(chunk_id, result);
                            self.spawn_chunk_downloads(cancellation.clone(), tracker.clone());
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
