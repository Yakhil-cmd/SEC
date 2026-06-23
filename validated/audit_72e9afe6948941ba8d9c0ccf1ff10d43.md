### Title
Unvalidated Opening Pool Unbounded Growth via Missing-Complaint Deferral — (`rs/consensus/idkg/src/complaints.rs`)

---

### Summary

The `opening_missing_complaint` branch in `validate_openings` silently defers openings for active transcripts that have no matching complaint in the validated pool. Combined with a `purge_artifacts` routine that only removes openings for **inactive** transcripts, and a P2P layer configured with `SLOT_TABLE_NO_LIMIT` for IDKG artifacts, a single Byzantine peer below the fault threshold can flood the unvalidated pool with openings that are never removed as long as the target transcript remains active.

---

### Finding Description

**The silent-defer branch (lines 252–257):**

When `validate_openings` processes an opening that maps to an active transcript (`Action::Process`) but finds no matching complaint in the validated pool via `get_complaint_for_opening`, it takes no action — no `RemoveUnvalidated`, no timeout, no `HandleInvalid`:

```rust
} else {
    // Defer handling the opening in case it was received
    // before the complaint.
    self.metrics
        .complaint_errors_inc("opening_missing_complaint");
}
``` [1](#0-0) 

**The purge gap:**

`purge_artifacts` removes unvalidated openings only when `should_purge` returns true, which requires the transcript to be **absent** from `active_transcripts`:

```rust
fn should_purge(...) -> bool {
    requested_height <= current_height && !active_transcripts.contains(transcript_id)
}
``` [2](#0-1) 

Openings for active transcripts (key transcripts, stashed pre-signature transcripts, transcripts paired with ongoing requests) are never purged by this path. [3](#0-2) 

**The bouncer provides no complaint-existence check:**

`IDkgBouncer`'s `compute_bouncer` for openings only checks height against `finalized_height + LOOK_AHEAD`. It does not verify whether a corresponding complaint exists:

```rust
IDkgMessageId::Opening(_, data) => {
    if data.get_ref().height <= args.finalized_height + Height::from(LOOK_AHEAD) {
        BouncerValue::Wants
    } else {
        BouncerValue::MaybeWantsLater
    }
}
``` [4](#0-3) 

**No per-peer slot table limit for IDKG:**

The IDKG artifact channel is registered with `SLOT_TABLE_NO_LIMIT = usize::MAX`, meaning the P2P receiver imposes no cap on how many distinct openings a single peer can advertise:

```rust
const SLOT_TABLE_NO_LIMIT: usize = usize::MAX;
// ...
new_p2p_consensus.abortable_broadcast_channel(assembler, SLOT_TABLE_NO_LIMIT)
``` [5](#0-4) [6](#0-5) 

**The spec divergence:**

The formal spec (`spec.md`) explicitly states that an opening without a matching complaint should return `Some(false)` (invalid → remove), not be deferred:

```
if (exists complaint in complaints: complaint.key() == dealing.key())
    return Some(true);
else
    return Some(false);  // <-- spec: invalid, remove it
``` [7](#0-6) 

The implementation instead defers indefinitely, diverging from the spec's intent.

---

### Impact Explanation

A Byzantine peer (one that is a legitimate subnet member but behaves maliciously, below the `f` fault threshold) can:

1. Craft many `IDkgMessage::Opening` messages for an active transcript (e.g., the long-lived key transcript) with different content hashes — each is a distinct pool entry since the pool key is the message hash.
2. Advertise them via the P2P slot protocol. With `SLOT_TABLE_NO_LIMIT`, the receiver accepts all of them.
3. Each opening passes the bouncer (height check only) and enters the unvalidated pool.
4. `validate_openings` hits the `opening_missing_complaint` branch for each and does nothing.
5. `purge_artifacts` never removes them because the transcript is active.

The unvalidated pool grows without bound on the targeted replica. Key transcripts can remain active indefinitely (as long as the subnet uses that key). The impact is **unbounded memory growth on a single replica**, potentially causing OOM or severe performance degradation, without affecting consensus correctness on other replicas.

---

### Likelihood Explanation

- **Attacker capability**: Must be a subnet peer (registered node). This is a realistic Byzantine adversary model — the IC explicitly tolerates up to `f` Byzantine nodes.
- **No threshold corruption required**: A single Byzantine peer suffices.
- **No crypto forgery required**: The openings need only pass the height-based bouncer; they are never cryptographically verified in the `opening_missing_complaint` branch.
- **Practical bound**: The number of unique `(transcript_id, dealer_id, opener_id)` tuples is bounded by subnet size × dealers, but since the pool key is the message hash, a Byzantine peer can generate arbitrarily many openings with the same structural key but different content, each stored separately.

---

### Recommendation

1. **Treat "no complaint" as invalid for active transcripts**: When `Action::Process` is returned (transcript is active and fully resolved) but no complaint exists in the validated pool, emit `IDkgChangeAction::HandleInvalid` or `RemoveUnvalidated` rather than silently deferring. This aligns with the spec's `Some(false)` return for this case.

2. **Add a per-peer slot table limit for IDKG**: Change `SLOT_TABLE_NO_LIMIT` to a bounded value for the IDKG channel (similar to `SLOT_TABLE_LIMIT_INGRESS = 50_000` for ingress), preventing any single peer from advertising unbounded artifacts.

3. **Add a complaint-existence check to the bouncer**: `IDkgBouncer::compute_bouncer` for openings could return `BouncerValue::Unwanted` if no matching complaint exists in the validated pool, preventing complaint-less openings from ever entering the unvalidated pool.

---

### Proof of Concept

```rust
// Pseudocode: Byzantine peer floods openings for active transcript T
// with no complaint in the validated pool.

let active_transcript_id = /* key transcript ID, known from chain */;
for i in 0..1_000_000 {
    let opening = craft_opening_with_nonce(active_transcript_id, dealer_id, opener_id, i);
    // opening.source_height <= finalized_height + LOOK_AHEAD → bouncer returns Wants
    // opening is inserted into unvalidated pool
    p2p_send(opening);
}

// On victim replica:
// validate_openings: hits opening_missing_complaint branch for each → no action
// purge_artifacts: transcript is active → no purge
// Result: 1,000,000 openings accumulate in unvalidated pool → OOM
```

The existing test `test_validate_openings` at line 1683 already documents the "without a matching complaint (deferred)" case as expected behavior, confirming the deferral is intentional but unguarded: [8](#0-7)

### Citations

**File:** rs/consensus/idkg/src/complaints.rs (L252-257)
```rust
                    } else {
                        // Defer handling the opening in case it was received
                        // before the complaint.
                        self.metrics
                            .complaint_errors_inc("opening_missing_complaint");
                    }
```

**File:** rs/consensus/idkg/src/complaints.rs (L313-328)
```rust
        // Unvalidated openings
        let mut action = idkg_pool
            .unvalidated()
            .openings()
            .filter(|(_, signed_opening)| {
                let opening = signed_opening.get();
                self.should_purge(
                    &opening.idkg_opening.transcript_id,
                    opening.idkg_opening.transcript_id.source_height(),
                    current_height,
                    &active_transcripts,
                )
            })
            .map(|(id, _)| IDkgChangeAction::RemoveUnvalidated(id))
            .collect();
        ret.append(&mut action);
```

**File:** rs/consensus/idkg/src/complaints.rs (L663-671)
```rust
    fn should_purge(
        &self,
        transcript_id: &IDkgTranscriptId,
        requested_height: Height,
        current_height: Height,
        active_transcripts: &BTreeSet<IDkgTranscriptId>,
    ) -> bool {
        requested_height <= current_height && !active_transcripts.contains(transcript_id)
    }
```

**File:** rs/consensus/idkg/src/complaints.rs (L1681-1689)
```rust
        // Opening for a transcript currently active,
        // without a matching complaint (deferred)
        let opening = create_opening(id_4, NODE_2, NODE_3, NODE_4);
        let msg_id_3 = opening.message_id();
        artifacts.push(UnvalidatedArtifact {
            message: IDkgMessage::Opening(opening),
            peer_id: NODE_4,
            timestamp: UNIX_EPOCH,
        });
```

**File:** rs/consensus/idkg/src/lib.rs (L611-617)
```rust
        IDkgMessageId::Opening(_, data) => {
            if data.get_ref().height <= args.finalized_height + Height::from(LOOK_AHEAD) {
                BouncerValue::Wants
            } else {
                BouncerValue::MaybeWantsLater
            }
        }
```

**File:** rs/replica/setup_ic_network/src/lib.rs (L75-75)
```rust
const SLOT_TABLE_NO_LIMIT: usize = usize::MAX;
```

**File:** rs/replica/setup_ic_network/src/lib.rs (L282-292)
```rust
        let idkg = {
            let assembler = ic_artifact_downloader::FetchArtifact::new(
                log.clone(),
                rt_handle.clone(),
                artifact_pools.idkg_pool.clone(),
                bouncers.idkg,
                metrics_registry.clone(),
            );

            new_p2p_consensus.abortable_broadcast_channel(assembler, SLOT_TABLE_NO_LIMIT)
        };
```

**File:** rs/consensus/idkg/spec.md (L1385-1392)
```markdown
                    if (exists complaint in complaints: complaint.key() == dealing.key())
                        // We only want to move an opening to the validated pool if there is
                        // a corresponding complaint. This is not strictly necessary, but it
                        // prevents us from relaying openings unnecessarily.
                        return Some(true);
                    else
                        return Some(false);

```
