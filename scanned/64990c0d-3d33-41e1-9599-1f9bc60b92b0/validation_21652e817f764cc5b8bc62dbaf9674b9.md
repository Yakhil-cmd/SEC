The code confirms the vulnerability is real. Here is the complete trace:

**`ChunksToDownload.download_failed`** has no retry counter, no backoff, and no limit — it unconditionally re-queues the chunk: [1](#0-0) 

**`handle_downloaded_chunk_result`** for `Overloaded` does not remove the peer from `active_downloads`, only re-queues the chunk: [2](#0-1) 

**`parse_chunk_handler_response`** maps HTTP 429 → `DownloadChunkError::Overloaded`: [3](#0-2) 

**`spawn_chunk_downloads`** will always re-select the sole remaining peer via `WeightedIndex`: [4](#0-3) 

The only loop exit conditions are external cancellation or `active_downloads.is_empty()`: [5](#0-4) 

`CHUNK_DOWNLOAD_TIMEOUT` is per-chunk only; there is no global state sync timeout: [6](#0-5) 

---

### Title
Byzantine Peer Permanent 429 Loop Causes Unbounded State Sync Stall — (`rs/p2p/state_sync_manager/src/ongoing.rs`)

### Summary
A Byzantine replica peer that always responds with HTTP 429 (`TOO_MANY_REQUESTS`) to chunk requests can hold a victim node's state sync in an infinite retry loop. Because `DownloadChunkError::Overloaded` does not evict the peer from `active_downloads`, and `ChunksToDownload::download_failed` unconditionally re-queues the chunk with no retry limit or backoff, the sync loops at full speed (10 concurrent slots, 10 s per-chunk timeout) forever, never making progress and never self-terminating.

### Finding Description
The state sync loop in `OngoingStateSync::run` exits only when the `CancellationToken` fires or `active_downloads` becomes empty. [7](#0-6) 

`active_downloads` shrinks only on `NoContent` or `RequestError`: [8](#0-7) 

`Overloaded` (and `Timeout`, `Cancelled`) leave the peer in the map and re-queue the chunk: [2](#0-1) 

`ChunksToDownload::download_failed` is a pure push with no counter: [1](#0-0) 

The per-chunk timeout of 10 s fires `DownloadChunkError::Timeout`, which also does not evict the peer, so even a slow-responding Byzantine peer achieves the same effect. [9](#0-8) 

The external escape via `cancel_if_running` in `StateSyncManager::handle_advert` is only triggered when a new advert arrives and the `StateSyncClient` implementation decides the sync should be cancelled — if the node is still behind, it returns `false` and the stalled sync continues. [10](#0-9) 

### Impact Explanation
The victim replica node cannot complete state sync, cannot advance its certified state height, and therefore cannot participate in consensus. The node is effectively partitioned from the subnet for as long as the Byzantine peer remains its sole `active_downloads` entry. This is a targeted liveness denial-of-service against a specific replica node.

### Likelihood Explanation
The scenario where a Byzantine peer is the last one in `active_downloads` is reachable in two ways:

1. The Byzantine peer is the first (or only) peer to advertise the state to the victim, and honest peers' adverts are delayed or dropped (unreliable broadcast is explicitly noted in the code). [11](#0-10) 

2. Honest peers are present initially but get evicted because they return `NoContent` (they have already pruned that state) or a `RequestError`, leaving only the Byzantine peer. [8](#0-7) 

Both paths require only a single Byzantine replica node — well below the fault threshold.

### Recommendation
- **Evict after N consecutive `Overloaded`/`Timeout` responses**: maintain a per-peer failure counter; remove the peer after a configurable threshold (e.g., 20 consecutive transient failures).
- **Global state sync deadline**: add a wall-clock timeout for the entire `OngoingStateSync` (e.g., tied to the CUP interval), after which the sync is cancelled and restarted.
- **Exponential backoff on `Overloaded`**: instead of immediately re-queuing, delay re-scheduling chunks from an overloaded peer, which also limits the tight busy-loop.

### Proof of Concept
The existing test `test_cancel_if_running` in `ongoing.rs` already demonstrates the exact scenario — a mock transport that always returns 429 — and shows the sync only terminates via external `shutdown()`. A state-machine test asserting termination *without* external cancellation within a bounded wall-clock window would fail, confirming the bug. [12](#0-11)

### Citations

**File:** rs/p2p/state_sync_manager/src/ongoing/chunks_to_download.rs (L26-28)
```rust
    pub(crate) fn download_failed(&mut self, chunk_id: ChunkId) {
        self.chunks.push(chunk_id);
    }
```

**File:** rs/p2p/state_sync_manager/src/ongoing.rs (L44-46)
```rust
const PARALLEL_CHUNK_DOWNLOADS: usize = 10;
const ONGOING_STATE_SYNC_CHANNEL_SIZE: usize = 200;
const CHUNK_DOWNLOAD_TIMEOUT: Duration = Duration::from_secs(10);
```

**File:** rs/p2p/state_sync_manager/src/ongoing.rs (L116-176)
```rust
        loop {
            select! {
                () = cancellation.cancelled() => {
                    break
                },
                Some(download_result) = self.downloading_chunks.join_next() => {
                    match download_result {
                        Ok((result, chunk_id)) => {
                            // We do a saturating sub here because it can happen (in rare cases) that a peer that just
                            // joined this sync was previously removed from the sync and still had outstanding downloads.
                            // As a consequence there is the possibiliy of an underflow. In the case where we close old
                            // download task while having active downloads we might start to undercount active downloads
                            // for this peer but this is acceptable since everything will be reset anyway every 5-10min
                            // when state sync restarts.
                            self.active_downloads
                                .entry(result.peer_id)
                                .and_modify(|v| *v = v.saturating_sub(1));
                            self.handle_downloaded_chunk_result(chunk_id, result);
                            self.spawn_chunk_downloads(cancellation.clone(), tracker.clone());
                        }
                        Err(err) => {
                            // If task panic we propagate but we allow tasks to be cancelled.
                            // Task can be cancelled if someone calls .abort()
                            if err.is_panic() {
                                std::panic::resume_unwind(err.into_panic());
                            } else {
                                error!(self.log, "Bug: JoinMap task was cancelled.");
                            }
                        }
                    }
                }
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
                }
            }

            debug_assert!(
                self.active_downloads.len() * PARALLEL_CHUNK_DOWNLOADS == self.allowed_downloads
            );

            // Collect metrics
            self.metrics
                .allowed_parallel_downloads
                .set(self.allowed_downloads as i64);
            self.metrics
                .peers_serving_state
                .set(self.active_downloads.len() as i64);
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

**File:** rs/p2p/state_sync_manager/src/ongoing.rs (L249-263)
```rust
        let mut peers = Vec::with_capacity(self.active_downloads.len());
        let mut weights = Vec::with_capacity(self.active_downloads.len());
        for (peer, active_downloads) in &self.active_downloads {
            peers.push(*peer);
            // Add one such that all peers can get selected.
            weights.push(max_active_downloads - active_downloads + 1);
        }
        let dist = WeightedIndex::new(weights).expect("weights>=0, sum(weights)>0, len(weigths)>0");

        for _ in 0..available_download_capacity {
            match self.chunks_to_download.next_chunk_to_download() {
                Some(chunk) => {
                    // Select random peer weighted proportional to active downloads.
                    // Peers with less active downloads are more likely to be selected.
                    let peer_id = *peers.get(dist.sample(&mut small_rng)).expect("Is present");
```

**File:** rs/p2p/state_sync_manager/src/ongoing.rs (L352-357)
```rust
            Err(_) => {
                return DownloadResult {
                    peer_id,
                    result: Err(DownloadChunkError::Timeout),
                };
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

**File:** rs/p2p/state_sync_manager/src/routes/chunk.rs (L129-129)
```rust
        StatusCode::TOO_MANY_REQUESTS => Err(DownloadChunkError::Overloaded),
```

**File:** rs/p2p/state_sync_manager/src/lib.rs (L159-163)
```rust
            } else {
                if self.state_sync.cancel_if_running(&ongoing.artifact_id) {
                    ongoing.shutdown.cancel();
                }
                return;
```

**File:** rs/p2p/state_sync_manager/src/lib.rs (L229-230)
```rust
            // Unreliable broadcast of adverts to all current peers.
            for (peer_id, _) in transport.peers() {
```
