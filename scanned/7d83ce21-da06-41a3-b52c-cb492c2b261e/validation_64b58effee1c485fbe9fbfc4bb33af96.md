### Title
Unbounded `crypto_verify_complaint` Calls via Deduplication Gap in `validate_complaints` — (`rs/consensus/idkg/src/complaints.rs`)

---

### Summary

`validate_complaints` iterates all unvalidated complaints without a per-round cap and calls `crypto_verify_complaint` for each `Action::Process` entry. The in-batch deduplication set `validated_complaints` is only updated on `MoveToValidated`, not on `HandleInvalid`. A Byzantine subnet member can therefore flood the unvalidated pool with N complaints sharing the same `ComplaintKey` but carrying different `internal_complaint_raw` bytes (valid signature, invalid MEGa proof), causing N sequential `crypto_verify_complaint` calls per `validate_complaints` invocation. Because IDKG artifacts are admitted with `SLOT_TABLE_NO_LIMIT` and the pool has no per-peer quota, the attack is sustained indefinitely.

---

### Finding Description

**Deduplication gap — the root cause**

`validate_complaints` maintains a local `validated_complaints: BTreeSet<ComplaintKey>` to skip duplicates within a batch:

```rust
if validated_complaints.contains(&key) {
    ret.push(IDkgChangeAction::RemoveUnvalidated(id));
    continue;
}
```

The set is updated **only** when `MoveToValidated` is returned:

```rust
let action = self.crypto_verify_complaint(id, transcript, signed_complaint);
if let Some(IDkgChangeAction::MoveToValidated(_)) = action {
    validated_complaints.insert(key);   // ← only on success
}
``` [1](#0-0) 

When `crypto_verify_complaint` returns `HandleInvalid` (permanent crypto failure), the key is **not** inserted. Every subsequent complaint in the same batch with the same `ComplaintKey` but a different message ID therefore bypasses the deduplication check and triggers another `crypto_verify_complaint` call.

**`crypto_verify_complaint` is a two-step operation**

Step 1 verifies the BLS/ECDSA signature; step 2 calls `self.crypto.verify_complaint(...)` which deserialises the MEGa public key and verifies the ZK proof: [2](#0-1) 

A Byzantine subnet member can produce complaints with a **valid signature** (step 1 passes) but **arbitrary `internal_complaint_raw`** (step 2 fails → `HandleInvalid`). Each such complaint has a unique hash → unique `IDkgMessageId` → unique pool entry.

**No per-peer quota for IDKG artifacts**

The P2P slot-table limit for IDKG is `SLOT_TABLE_NO_LIMIT = usize::MAX`, unlike ingress which is capped at 50 000: [3](#0-2) [4](#0-3) 

`IDkgPoolImpl::insert` performs no size or per-peer check: [5](#0-4) 

The `IDkgBouncer` for complaints only filters by height, not by count: [6](#0-5) 

**No per-round processing cap**

`validate_complaints` iterates the entire unvalidated complaint iterator in one call with no early exit or batch limit: [7](#0-6) 

**`has_complainer_issued_complaint` does not block re-injection**

This guard checks the **validated** pool. Because `HandleInvalid` complaints are removed from the unvalidated pool but never added to the validated pool, the guard returns `false` for every re-injected complaint with the same key: [8](#0-7) 

**Outer `RoundRobin` does not prevent the attack**

`IDkgImpl::on_state_change` uses `RoundRobin` over `[pre_signer, signer, complaint_handler]`, and the complaint handler's inner `on_state_change` uses another `RoundRobin` over `[validate_complaints, send_openings, validate_openings]`. When `validate_complaints` is invoked it processes the entire pool in one synchronous call with no time budget: [9](#0-8) [10](#0-9) 

---

### Impact Explanation

A single Byzantine subnet member (below the BFT fault threshold) can:

1. Continuously inject N complaints per round, each with a valid signature but invalid MEGa proof and a unique `internal_complaint_raw`.
2. Force `validate_complaints` to call `crypto_verify_complaint` N times per invocation (two crypto operations each: BLS verify + MEGa key deserialisation + ZK proof check).
3. After each batch drains the pool, re-inject N fresh complaints (different bytes → different message IDs → new pool entries).

The result is sustained CPU exhaustion on the complaint-handler thread, degrading IDKG throughput (threshold signing, key resharing). The `pre_signer` and `signer` subcomponents are starved during the rounds in which `validate_complaints` holds the CPU.

---

### Likelihood Explanation

- Requires only a single Byzantine subnet member with a valid signing key — well within the BFT fault assumption.
- No special network position, no threshold corruption, no admin access.
- The attack is fully local to the P2P/consensus layer and requires no ingress or canister interaction.
- `SLOT_TABLE_NO_LIMIT` removes the only natural throttle.

---

### Recommendation

1. **Fix the deduplication gap**: Insert the `ComplaintKey` into `validated_complaints` on **both** `MoveToValidated` and `HandleInvalid`, so that all complaints with the same key are deduplicated within a single batch regardless of outcome.

2. **Add a per-round processing cap**: Limit `validate_complaints` to at most `K` crypto operations per invocation (e.g. `K = 2 * subnet_size`), deferring the remainder to the next round.

3. **Add a per-peer slot quota for IDKG artifacts**: Mirror the ingress pool's `SLOT_TABLE_LIMIT_INGRESS` pattern for IDKG complaints and openings.

4. **Pre-filter by complainer membership**: Before calling `crypto_verify_complaint`, verify that `signed_complaint.signature.signer` is a receiver of the transcript. This is a cheap registry lookup that rejects structurally invalid complaints before the expensive MEGa proof check.

---

### Proof of Concept

```rust
// Byzantine node B (valid subnet member):
// For each active transcript T with dealer D:
for nonce in 0..N {
    let complaint = IDkgComplaint {
        transcript_id: T.transcript_id,
        dealer_id: D,
        internal_complaint_raw: vec![nonce as u8; 32], // unique, invalid MEGa proof
    };
    let content = IDkgComplaintContent { idkg_complaint: complaint };
    // Sign with B's real key → valid signature, invalid content
    let signed = crypto_b.sign(&content, B_node_id, T.registry_version);
    p2p_send(signed); // admitted: SLOT_TABLE_NO_LIMIT, height check passes
}
// Each validate_complaints call: N × crypto_verify_complaint
// Step 1 (sig verify): passes. Step 2 (MEGa proof): fails → HandleInvalid.
// validated_complaints never updated → no deduplication → N calls every batch.
// Byzantine node re-injects N new complaints after each drain.
```

A benchmark asserting that `validate_complaints` runtime is bounded by `O(num_active_transcripts × subnet_size)` regardless of unvalidated pool size would demonstrate the violation.

### Citations

**File:** rs/consensus/idkg/src/complaints.rs (L118-158)
```rust
        let mut validated_complaints = BTreeSet::new();

        let mut ret = Vec::new();
        for (id, signed_complaint) in idkg_pool.unvalidated().complaints() {
            let complaint = signed_complaint.get();
            // Remove the duplicate entries
            let key = ComplaintKey::from(&signed_complaint);
            if validated_complaints.contains(&key) {
                self.metrics
                    .complaint_errors_inc("duplicate_complaints_in_batch");
                ret.push(IDkgChangeAction::RemoveUnvalidated(id));
                continue;
            }

            match Action::action(
                block_reader,
                active_transcripts,
                &requested_transcripts,
                complaint.idkg_complaint.transcript_id.source_height(),
                &complaint.idkg_complaint.transcript_id,
            ) {
                Action::Process(transcript) => {
                    if self.has_complainer_issued_complaint(
                        idkg_pool,
                        &complaint.idkg_complaint,
                        &signed_complaint.signature.signer,
                    ) {
                        self.metrics.complaint_errors_inc("duplicate_complaint");
                        ret.push(IDkgChangeAction::RemoveUnvalidated(id));
                    } else {
                        let action = self.crypto_verify_complaint(id, transcript, signed_complaint);
                        if let Some(IDkgChangeAction::MoveToValidated(_)) = action {
                            validated_complaints.insert(key);
                        }
                        ret.append(&mut action.into_iter().collect());
                    }
                }
                Action::Drop => ret.push(IDkgChangeAction::RemoveUnvalidated(id)),
                Action::Defer => {}
            }
        }
```

**File:** rs/consensus/idkg/src/complaints.rs (L386-448)
```rust
        // Verify the signature
        if let Err(error) = self
            .crypto
            .verify(&signed_complaint, transcript.registry_version)
        {
            if error.is_reproducible() {
                self.metrics
                    .complaint_errors_inc("verify_complaint_signature_permanent");
                return Some(IDkgChangeAction::HandleInvalid(
                    id,
                    format!(
                        "Complaint signature validation(permanent error): {signed_complaint}, error = {error:?}"
                    ),
                ));
            } else {
                // Defer in case of transient errors
                warn!(
                    every_n_seconds => 10,
                    self.log,
                    "Complaint signature validation(transient error): {}, error = {:?}",
                    signed_complaint,
                    error
                );
                self.metrics
                    .complaint_errors_inc("verify_complaint_signature_transient");
                return None;
            }
        }

        match self.crypto.verify_complaint(
            transcript,
            signed_complaint.signature.signer,
            &complaint.idkg_complaint,
        ) {
            Err(error) if error.is_reproducible() => {
                self.metrics
                    .complaint_errors_inc("verify_complaint_permanent");
                Some(IDkgChangeAction::HandleInvalid(
                    id,
                    format!(
                        "Complaint validation(permanent error): {signed_complaint}, error = {error:?}"
                    ),
                ))
            }
            Err(error) => {
                warn!(
                    every_n_seconds => 10,
                    self.log,
                    "Complaint validation(transient error): {}, error = {:?}",
                    signed_complaint,
                    error
                );
                self.metrics
                    .complaint_errors_inc("verify_complaint_transient");
                None
            }
            Ok(()) => {
                self.metrics.complaint_metrics_inc("complaint_received");
                Some(IDkgChangeAction::MoveToValidated(IDkgMessage::Complaint(
                    signed_complaint,
                )))
            }
        }
```

**File:** rs/consensus/idkg/src/complaints.rs (L579-599)
```rust
    fn has_complainer_issued_complaint(
        &self,
        idkg_pool: &dyn IDkgPool,
        idkg_complaint: &IDkgComplaint,
        complainer_id: &NodeId,
    ) -> bool {
        let prefix = complaint_prefix(
            &idkg_complaint.transcript_id,
            &idkg_complaint.dealer_id,
            complainer_id,
        );
        idkg_pool
            .validated()
            .complaints_by_prefix(prefix)
            .any(|(_, signed_complaint)| {
                let complaint = signed_complaint.get();
                signed_complaint.signature.signer == *complainer_id
                    && complaint.idkg_complaint.transcript_id == idkg_complaint.transcript_id
                    && complaint.idkg_complaint.dealer_id == idkg_complaint.dealer_id
            })
    }
```

**File:** rs/consensus/idkg/src/complaints.rs (L844-847)
```rust
        let calls: [&'_ dyn Fn() -> IDkgChangeSet; 3] =
            [&validate_complaints, &send_openings, &validate_openings];

        changes.append(&mut schedule.call_next(&calls));
```

**File:** rs/replica/setup_ic_network/src/lib.rs (L74-75)
```rust
const SLOT_TABLE_LIMIT_INGRESS: usize = 50_000;
const SLOT_TABLE_NO_LIMIT: usize = usize::MAX;
```

**File:** rs/replica/setup_ic_network/src/lib.rs (L291-291)
```rust
            new_p2p_consensus.abortable_broadcast_channel(assembler, SLOT_TABLE_NO_LIMIT)
```

**File:** rs/artifact_pool/src/idkg_pool.rs (L475-479)
```rust
    fn insert(&mut self, artifact: UnvalidatedArtifact<IDkgMessage>) {
        let mut ops = IDkgPoolSectionOps::new();
        ops.insert(artifact.into_inner());
        self.unvalidated.mutate(ops);
    }
```

**File:** rs/consensus/idkg/src/lib.rs (L484-485)
```rust
        let calls: [&'_ dyn Fn() -> IDkgChangeSet; 3] = [&pre_signer, &signer, &complaint_handler];
        let ret = self.schedule.call_next(&calls);
```

**File:** rs/consensus/idkg/src/lib.rs (L604-610)
```rust
        IDkgMessageId::Complaint(_, data) => {
            if data.get_ref().height <= args.finalized_height + Height::from(LOOK_AHEAD) {
                BouncerValue::Wants
            } else {
                BouncerValue::MaybeWantsLater
            }
        }
```
