## Analysis

I traced the full code path to determine whether this is a real, exploitable vulnerability.

### Bouncer Logic — No Upper Bound

`CertifierBouncer::new_bouncer` returns `BouncerValue::Wants` for every `CertificationMessageId` whose height is `>= cup_height` and not in `certified_heights`. There is no upper-bound check. [1](#0-0) 

### Unvalidated Pool — No Size Limit

`CertificationPoolImpl::insert` places artifacts into an unbounded `BTreeMap` with no admission control or size cap. [2](#0-1) 

Compare this to the ingress pool, which has an explicit `exceeds_limit` guard: [3](#0-2) 

No equivalent guard exists for the certification pool.

### Validation Only Processes State-Manager Heights

`CertifierImpl::on_state_change` only validates artifacts at heights returned by `list_state_hashes_to_certify` / `list_state_heights_to_certify`. For heights far above the current tip, the state manager has no state, so those heights are never in the list. [4](#0-3) [5](#0-4) 

Artifacts at future heights therefore sit in the unvalidated pool indefinitely — never validated, never moved to validated, never removed.

### Purging Only Removes Below CUP Height

`get_purge_height` only purges artifacts *below* `cup_height - MINIMUM_CHAIN_LENGTH`. Artifacts at heights *above* `cup_height` are never purged. [6](#0-5) 

### Contrast With Consensus Bouncer

The consensus bouncer explicitly uses `MaybeWantsLater` for artifacts too far ahead of the current CUP height (`ACCEPTABLE_NOTARIZATION_CUP_GAP = 130`, `LOOK_AHEAD = 10`): [7](#0-6) 

The certification bouncer has no analogous protection.

### Download Path Confirms the Flow

`FetchArtifact::download_artifact` downloads any artifact for which the bouncer returns `Wants`, verifies only that `message.id() == id`, and delivers it to the pool — no further admission control. [8](#0-7) [9](#0-8) 

A Byzantine peer can craft a `CertificationShare` at any height, compute its hash, advertise the corresponding `CertificationMessageId`, and serve the artifact when fetched. The ID check passes because the attacker controls both the advert and the served artifact.

---

### Title
Unbounded Certification Pool Growth via Missing Upper-Bound in `CertifierBouncer::new_bouncer` — (`rs/consensus/certification/src/certifier.rs`)

### Summary
`CertifierBouncer::new_bouncer` returns `BouncerValue::Wants` for all `CertificationMessageId` values at heights above `cup_height` with no upper-bound check. A Byzantine subnet peer can advertise arbitrarily many `CertificationMessageId` values at heights far above the current tip. The honest replica downloads and inserts them into an unbounded unvalidated pool. These artifacts are never validated (the state manager has no state at those heights) and never purged (purging only applies below `cup_height`), causing unbounded memory growth.

### Finding Description
The `CertifierBouncer` at `certifier.rs:81–96` has a two-condition filter: drop if `height < cup_height` or if `certified_heights.contains(&height)`. There is no upper-bound condition. The unvalidated pool (`CertificationPoolImpl`) is a plain `BTreeMap` with no size limit. `CertifierImpl::on_state_change` only validates heights present in `state_manager.list_state_hashes_to_certify()` / `list_state_heights_to_certify()`, which are bounded by the local state tip. `get_purge_height` only purges below `cup_height - MINIMUM_CHAIN_LENGTH`. Artifacts at heights above the tip accumulate permanently.

### Impact Explanation
Unbounded memory growth in the unvalidated certification pool on every honest replica that peers with the attacker. Sustained attack leads to OOM, replica crash, certification pipeline stall, blocked xnet message delivery, and unavailable state sync.

### Likelihood Explanation
Requires a Byzantine peer within the subnet (below the fault threshold `f`). The attacker does not need a majority — a single compromised node suffices. The attack is cheap: advertising IDs costs nothing, and serving crafted `CertificationShare` artifacts is trivial. The attack is persistent because the pool never drains.

### Recommendation
Add an upper-bound check in `CertifierBouncer::new_bouncer`, analogous to the consensus bouncer's `ACCEPTABLE_NOTARIZATION_CUP_GAP` / `LOOK_AHEAD` guards. For example, return `BouncerValue::Unwanted` (or `MaybeWantsLater`) for heights more than a bounded window above `cup_height`. Additionally, add a size cap to the unvalidated certification pool with per-peer accounting, similar to the ingress pool's `exceeds_limit` mechanism.

### Proof of Concept
1. Byzantine peer crafts `N` distinct `CertificationShare` structs at heights `cup_height+1` through `cup_height+N`.
2. Peer advertises the corresponding `CertificationMessageId` values to an honest replica.
3. Honest replica's `CertifierBouncer` returns `Wants` for all `N` IDs (no upper bound).
4. `FetchArtifact` fetches all `N` artifacts; each passes the `message.id() == id` check.
5. All `N` artifacts are inserted into `CertificationPoolImpl.unvalidated` (unbounded `BTreeMap`).
6. `CertifierImpl::on_state_change` never validates them (state manager has no state at those heights).
7. `get_purge_height` never removes them (purging only goes below `cup_height`).
8. Repeat with new heights as `cup_height` advances; pool grows without bound.

### Citations

**File:** rs/consensus/certification/src/certifier.rs (L86-95)
```rust
        Box::new(move |id| {
            let height = id.height;
            // We drop all artifacts below the CUP height or those for which we have a full
            // certification already.
            if height < cup_height || certified_heights.contains(&height) {
                BouncerValue::Unwanted
            } else {
                BouncerValue::Wants
            }
        })
```

**File:** rs/consensus/certification/src/certifier.rs (L161-178)
```rust
        let state_hashes_to_certify: Vec<_> = self
            .state_manager
            .list_state_hashes_to_certify()
            .into_iter()
            .filter(|state_hash_metadata| !deliver_state_certification(state_hash_metadata.height))
            .collect();
        trace!(
            &self.log,
            "Received {} hash(es) to be certified in {:?}",
            state_hashes_to_certify.len(),
            start.elapsed()
        );
        let state_heights_to_certify: Vec<_> = self
            .state_manager
            .list_state_heights_to_certify()
            .into_iter()
            .filter(|height| !deliver_state_certification(*height))
            .collect();
```

**File:** rs/consensus/certification/src/certifier.rs (L240-244)
```rust
        let start = Instant::now();
        let change_set = self.validate(
            certification_pool,
            state_heights_to_aggregate_and_validate.into_iter(),
        );
```

**File:** rs/consensus/certification/src/certifier.rs (L463-474)
```rust
    fn get_purge_height(&self) -> Option<Height> {
        let cup_height = self.consensus_pool_cache.catch_up_package().height();
        // We pick cup_height, but retain at least the last MINIMUM_CHAIN_LENGTH heights
        let purge_height = Height::from(cup_height.get().saturating_sub(MINIMUM_CHAIN_LENGTH));

        let mut prev_highest_purged_height = self.highest_purged_height.borrow_mut();
        if *prev_highest_purged_height < purge_height {
            *prev_highest_purged_height = purge_height;
            return Some(purge_height);
        }
        None
    }
```

**File:** rs/artifact_pool/src/certification_pool.rs (L259-272)
```rust
    fn insert(&mut self, msg: UnvalidatedArtifact<CertificationMessage>) {
        let hash = CertificationMessageHash::from(&msg.message);

        if match hash {
            CertificationMessageHash::Certification(_) => self
                .unvalidated_cert_index
                .insert(msg.message.height(), &hash),
            CertificationMessageHash::CertificationShare(_) => self
                .unvalidated_share_index
                .insert(msg.message.height(), &hash),
        } {
            self.unvalidated.insert(hash, msg.message);
        }
    }
```

**File:** rs/artifact_pool/src/ingress_pool.rs (L226-232)
```rust
    fn exceeds_limit(&self, peer_id: &NodeId) -> bool {
        let counters = self.unvalidated.peer_counters.get_counters(peer_id)
            + self.validated.peer_counters.get_counters(peer_id);

        counters.bytes > self.ingress_pool_max_bytes
            || counters.messages > self.ingress_pool_max_count
    }
```

**File:** rs/consensus/src/consensus/priority.rs (L54-60)
```rust
    // Stash non-CUP artifacts, as long as they're too far ahead of the next pending CUP height.
    // This prevents nodes that have fallen behind from exceeding their validated pool bounds.
    if !matches!(id.hash, ConsensusMessageHash::CatchUpPackage(_))
        && height > next_cup_height + Height::new(ACCEPTABLE_NOTARIZATION_CUP_GAP)
    {
        return MaybeWantsLater;
    }
```

**File:** rs/p2p/artifact_downloader/src/fetch_artifact/download.rs (L211-214)
```rust
        // Evaluate bouncer and wait until we should fetch.
        if !Self::should_download(&id, &mut artifact, &metrics, &mut bouncer_watcher).await {
            return AssembleResult::Unwanted;
        }
```

**File:** rs/p2p/artifact_downloader/src/fetch_artifact/download.rs (L252-258)
```rust
                                if let Ok(message) = Artifact::PbMessage::proxy_decode(&body) {
                                    if message.id() == id {
                                        break AssembleResult::Done {
                                            message,
                                            peer_id: peer,
                                        };
                                    } else {
```
