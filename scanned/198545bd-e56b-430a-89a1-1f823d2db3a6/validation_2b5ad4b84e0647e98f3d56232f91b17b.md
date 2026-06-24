I have the full code. Let me analyze the exact execution path carefully.

The underflow is real and the attack path is concrete. Here is the precise trace:

**Step-by-step execution:**

1. Peer P added → `active_downloads = {P: 0}`, `allowed_downloads = 10` [1](#0-0) 

2. 10 chunks dispatched → `active_downloads = {P: 10}`, `allowed_downloads = 10`

3. First task completes with `NoContent`:
   - Run loop applies `saturating_sub(1)` to per-peer counter → `active_downloads = {P: 9}`
   - `handle_downloaded_chunk_result`: `active_downloads.remove(&P)` returns `Some(9)` → `allowed_downloads -= 10` → `allowed_downloads = 0`, `active_downloads = {}` [2](#0-1) [3](#0-2) 

4. 9 stale tasks remain in `downloading_chunks`.

5. P re-advertises → `Entry::Vacant` succeeds (P was removed) → `active_downloads = {P: 0}`, `allowed_downloads = 10` [4](#0-3) 

6. A stale task completes with `NoContent`:
   - Run loop: `active_downloads.entry(P).and_modify(|v| *v = v.saturating_sub(1))` → P now exists, `0.saturating_sub(1) = 0` → `active_downloads = {P: 0}` (no underflow here)
   - `handle_downloaded_chunk_result`: `active_downloads.remove(&P)` returns **`Some(0)`** → `is_some()` is `true` → `allowed_downloads -= 10` → **`0 - 10` underflows** [3](#0-2) 

The root cause: the `saturating_sub` fix in the run loop only protects the per-peer `u64` counter. The `allowed_downloads -= PARALLEL_CHUNK_DOWNLOADS` in `handle_downloaded_chunk_result` uses plain subtraction and is guarded only by `active_downloads.remove(&peer_id).is_some()` — which succeeds because the peer was re-added in step 5. The code comment at lines 124–129 explicitly acknowledges the stale-task scenario but the fix is incomplete. [5](#0-4) 

**Impact correction vs. the question's claim:**

The claim that this spawns `usize::MAX` tasks is overstated. `spawn_chunk_downloads` computes `available_download_capacity = allowed_downloads.saturating_sub(downloading_chunks.len())` ≈ `usize::MAX`, but the inner loop breaks immediately when `chunks_to_download.next_chunk_to_download()` returns `None`. [6](#0-5) 

The actual impacts are:
- **Debug/test builds**: the `debug_assert` at line 162 fires → panic.
- **Release builds**: `allowed_downloads` silently wraps to `usize::MAX`; if P is the only peer, `active_downloads` is now empty and state sync halts immediately (line 173–176); if other peers exist, the broken counter causes all remaining chunks to be dispatched in one burst (bounded by actual chunk count, not `usize::MAX`). [7](#0-6) 

---

### Title
`allowed_downloads` integer underflow via stale-task/peer-re-add race in `handle_downloaded_chunk_result` — (`rs/p2p/state_sync_manager/src/ongoing.rs`)

### Summary
A single malicious peer can trigger a `usize` underflow in `allowed_downloads` by: advertising a state (getting added), causing a `NoContent` response (getting removed), re-advertising (getting re-added), then having a stale in-flight task from the first session complete with `NoContent`. The `handle_downloaded_chunk_result` function uses plain subtraction for `allowed_downloads` and its only guard (`active_downloads.remove(&peer_id).is_some()`) succeeds because the peer was re-added, making the subtraction proceed on a zero value.

### Finding Description
`OngoingStateSync` tracks download concurrency with two coupled fields: `active_downloads: HashMap<NodeId, u64>` (per-peer in-flight count) and `allowed_downloads: usize` (total budget = `active_downloads.len() * PARALLEL_CHUNK_DOWNLOADS`). When a task completes, the run loop applies `saturating_sub(1)` to the per-peer counter, then calls `handle_downloaded_chunk_result`. That function removes the peer on `NoContent`/`RequestError` and subtracts `PARALLEL_CHUNK_DOWNLOADS` from `allowed_downloads` using plain `-`. The code comment at lines 124–129 acknowledges the stale-task scenario and applies `saturating_sub` to the per-peer counter, but does **not** extend the same protection to `allowed_downloads`. When a stale task fires after the peer has been re-added, `active_downloads.remove` succeeds (returning `Some`), and the plain subtraction underflows.

### Impact Explanation
In release builds (production), `allowed_downloads` wraps to `usize::MAX`. If the underflowing peer is the only peer, `active_downloads` becomes empty and state sync halts for the current cycle (disruption, not crash). If other peers are present, `spawn_chunk_downloads` computes an effectively unbounded `available_download_capacity`, but the loop is bounded by `chunks_to_download` — so the actual effect is a burst dispatch of all remaining chunks rather than `usize::MAX` tasks. In debug builds the `debug_assert` at line 162 panics. Either way the accounting invariant `allowed_downloads == active_downloads.len() * PARALLEL_CHUNK_DOWNLOADS` is permanently broken for the lifetime of the sync.

### Likelihood Explanation
Requires one malicious replica peer that can: (1) advertise a state, (2) respond with `NoContent` to one chunk request, (3) re-advertise the same state before the remaining in-flight tasks complete, (4) respond with `NoContent` again on a stale task. All four steps are within normal protocol peer behavior and require no privileged access or threshold corruption.

### Recommendation
Replace the plain subtraction in `handle_downloaded_chunk_result` with `saturating_sub`, mirroring the fix already applied to the per-peer counter:

```rust
// lines 196 and 207
self.allowed_downloads = self.allowed_downloads.saturating_sub(PARALLEL_CHUNK_DOWNLOADS);
```

Alternatively, add a guard that checks whether the removed peer's entry was from the current session (e.g., by tagging entries with a generation counter) before decrementing `allowed_downloads`.

### Proof of Concept
```rust
// Unit test sketch (single-threaded, no real transport needed):
// 1. add_peer(P)          → allowed_downloads = 10
// 2. dispatch 10 chunks to P
// 3. complete chunk_0 with NoContent → remove P, allowed_downloads = 0
// 4. add_peer(P) again    → allowed_downloads = 10
// 5. complete chunk_1 (stale) with NoContent
//    → active_downloads.remove(&P) = Some(0) → allowed_downloads -= 10 → UNDERFLOW
// assert_eq!(allowed_downloads, active_downloads.len() * PARALLEL_CHUNK_DOWNLOADS); // FAILS
```

### Citations

**File:** rs/p2p/state_sync_manager/src/ongoing.rs (L124-133)
```rust
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
```

**File:** rs/p2p/state_sync_manager/src/ongoing.rs (L148-158)
```rust
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

**File:** rs/p2p/state_sync_manager/src/ongoing.rs (L162-176)
```rust
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

**File:** rs/p2p/state_sync_manager/src/ongoing.rs (L194-197)
```rust
            Err(DownloadChunkError::NoContent) => {
                if self.active_downloads.remove(&peer_id).is_some() {
                    self.allowed_downloads -= PARALLEL_CHUNK_DOWNLOADS;
                }
```

**File:** rs/p2p/state_sync_manager/src/ongoing.rs (L239-284)
```rust
        let available_download_capacity = self
            .allowed_downloads
            .saturating_sub(self.downloading_chunks.len());

        let mut small_rng = SmallRng::from_entropy();
        let max_active_downloads = self
            .active_downloads
            .values()
            .max()
            .expect("Peers not empty");
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

                    self.active_downloads.entry(peer_id).and_modify(|v| *v += 1);
                    self.downloading_chunks.spawn_on(
                        chunk,
                        self.metrics
                            .download_task_monitor
                            .instrument(Self::download_chunk_task(
                                peer_id,
                                self.transport.clone(),
                                tracker.clone(),
                                self.artifact_id.clone(),
                                chunk,
                                cancellation.child_token(),
                                self.metrics.clone(),
                            )),
                        &self.rt,
                    );
                }
                None => break,
            }
        }
```
