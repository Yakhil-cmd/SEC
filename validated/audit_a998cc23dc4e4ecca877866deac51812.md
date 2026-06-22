Based on my analysis of the actual production code, here is my assessment:

---

### Title
Byzantine Peer Exploiting `DownloadChunkError::Overloaded` to Permanently Stall State Sync — (`rs/p2p/state_sync_manager/src/ongoing.rs`)

### Summary
A Byzantine peer that persistently returns HTTP 429 (`TOO_MANY_REQUESTS`) is never evicted from `active_downloads`, causing the downloading replica to loop indefinitely re-queuing and re-assigning the same chunks to the same peer, permanently stalling state sync with no self-healing mechanism.

### Finding Description

The asymmetric peer-removal logic in `handle_downloaded_chunk_result` is the root cause.

**`NoContent` and `RequestError` remove the peer:** [1](#0-0) 

**`Overloaded` (and `Timeout`/`Cancelled`) do NOT remove the peer — only re-queue the chunk:** [2](#0-1) 

`parse_chunk_handler_response` maps `StatusCode::TOO_MANY_REQUESTS` directly to `DownloadChunkError::Overloaded`: [3](#0-2) 

`download_failed` simply re-pushes the chunk ID back onto the queue with no retry counter, no backoff, and no penalty: [4](#0-3) 

The `run` loop only terminates if `active_downloads` becomes empty: [5](#0-4) 

Since `Overloaded` never removes the peer, `active_downloads` never empties (when the Byzantine peer is the only/dominant peer), and the loop runs forever. Each completed download (with 429) decrements the counter, `spawn_chunk_downloads` immediately re-assigns up to `PARALLEL_CHUNK_DOWNLOADS = 10` new tasks to the same peer, and the cycle repeats with no bound: [6](#0-5) 

There is no retry limit, no exponential backoff, no consecutive-failure counter, and no peer-score/reputation system within the state sync manager.

### Impact Explanation
A replica that is behind (e.g., after a restart or network partition) must state-sync to catch up. If the only peer(s) advertising the target state are Byzantine and always return 429, the replica's state sync loop runs indefinitely — the replica never catches up, never participates in consensus, and is effectively partitioned from the subnet for the duration of the attack. The `CHUNK_DOWNLOAD_TIMEOUT` of 10 seconds per attempt means the tight loop also wastes CPU/network resources on the victim: [7](#0-6) 

### Likelihood Explanation
- The attacker must control a subnet node (or a node that is the sole/dominant advertiser of a given state height).
- The attack requires no privileged key or governance majority — a single Byzantine node that is the first (or only) to advertise a state to a lagging replica is sufficient.
- The `Overloaded` path is explicitly designed as "transient" (comment at line 390), making the missing eviction logic a deliberate design choice that a Byzantine peer can exploit indefinitely. [8](#0-7) 

### Recommendation
1. **Track consecutive `Overloaded` responses per peer.** After a configurable threshold (e.g., 20 consecutive 429s), treat the peer as `RequestError` and remove it from `active_downloads`.
2. **Introduce exponential backoff** before re-assigning chunks to a peer that returned `Overloaded`, so the tight loop cannot spin at full speed.
3. **Add an overall state sync deadline** independent of per-chunk timeouts, so a stalled sync is eventually abandoned and restarted (potentially with a different peer set).

### Proof of Concept
The existing test `test_cancel_if_running` in `ongoing.rs` already uses a mock transport that always returns `StatusCode::TOO_MANY_REQUESTS` — it only passes because the test manually calls `shutdown`. A state-machine test without the manual shutdown would loop forever, confirming the stall: [9](#0-8)

### Citations

**File:** rs/p2p/state_sync_manager/src/ongoing.rs (L44-44)
```rust
const PARALLEL_CHUNK_DOWNLOADS: usize = 10;
```

**File:** rs/p2p/state_sync_manager/src/ongoing.rs (L46-46)
```rust
const CHUNK_DOWNLOAD_TIMEOUT: Duration = Duration::from_secs(10);
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

**File:** rs/p2p/state_sync_manager/src/ongoing.rs (L390-393)
```rust
    /// Request was not processed because peer endpoint is overloaded.
    /// This error is transient.
    #[error("overloaded")]
    Overloaded,
```

**File:** rs/p2p/state_sync_manager/src/ongoing.rs (L429-460)
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
```

**File:** rs/p2p/state_sync_manager/src/routes/chunk.rs (L129-129)
```rust
        StatusCode::TOO_MANY_REQUESTS => Err(DownloadChunkError::Overloaded),
```

**File:** rs/p2p/state_sync_manager/src/ongoing/chunks_to_download.rs (L26-28)
```rust
    pub(crate) fn download_failed(&mut self, chunk_id: ChunkId) {
        self.chunks.push(chunk_id);
    }
```
