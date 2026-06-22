The code is clear. Let me trace the exact logic path to confirm or deny the claimed vulnerability.

Based on my thorough code analysis, I can now deliver a definitive assessment.

**Exact code trace:**

`validate_complaints` (line 118): `validated_complaints` starts as an empty `BTreeSet`.

For each complaint in the unvalidated pool:
- Line 125: dedup check — `validated_complaints.contains(&key)` — only catches keys already inserted this batch
- Line 140: `has_complainer_issued_complaint` — queries **`idkg_pool.validated()`** only (line 591), not the unvalidated pool
- Line 148: `crypto_verify_complaint` is called
- Lines 149–150: key is inserted into `validated_complaints` **only** when the return value is `Some(IDkgChangeAction::MoveToValidated(_))`

When `crypto_verify_complaint` returns:
- `None` (transient error, line 411 or 440) → key **not** inserted
- `Some(HandleInvalid(...))` (permanent error, line 394 or 423) → key **not** inserted

In both non-success cases, the next complaint with the same `ComplaintKey` passes both guards and reaches `crypto_verify_complaint` again.

**Existing test gap:** `test_ecdsa_duplicate_complaints_in_batch` (line 1459) uses `CryptoReturningOk` — the mock that always succeeds. It only validates the happy path where the first complaint succeeds and the second is correctly deduplicated. There is no test covering the failure path.

**Attacker capability:** A Byzantine subnet node (valid entry point per the rules) can craft K complaints sharing the same `(transcript_id, dealer_id, complainer_id)` but with different `internal_complaint_raw` bytes. If they use valid signatures (their own key) but invalid complaint content, each complaint passes signature verification and reaches the expensive `verify_complaint` call, which fails permanently. Since `HandleInvalid` does not insert the key, all K complaints are crypto-verified.

---

### Title
Incomplete batch deduplication in `validate_complaints` allows a Byzantine node to amplify `crypto_verify_complaint` invocations — (`rs/consensus/idkg/src/complaints.rs`)

### Summary
`validate_complaints` tracks seen `ComplaintKey` values in a local `BTreeSet` that is only updated on successful validation (`MoveToValidated`). When the first complaint for a given key fails — either with a transient error (`None`) or a permanent error (`HandleInvalid`) — the key is never recorded, so every subsequent complaint with the same key bypasses both deduplication guards and is independently submitted to `crypto_verify_complaint`. A Byzantine subnet node can exploit this to multiply expensive threshold-crypto verification work on every honest replica.

### Finding Description

In `validate_complaints`:

```rust
// line 118
let mut validated_complaints = BTreeSet::new();

// line 149-150 — key inserted ONLY on MoveToValidated
if let Some(IDkgChangeAction::MoveToValidated(_)) = action {
    validated_complaints.insert(key);
}
``` [1](#0-0) [2](#0-1) 

The second guard, `has_complainer_issued_complaint`, queries only the **validated pool**:

```rust
// line 590-592
idkg_pool
    .validated()
    .complaints_by_prefix(prefix)
``` [3](#0-2) 

Neither guard covers the case where a prior complaint with the same key failed in the current batch. `crypto_verify_complaint` returns `None` on transient errors (lines 411, 440) and `Some(HandleInvalid(...))` on permanent errors (lines 394, 423) — neither causes the key to be recorded: [4](#0-3) [5](#0-4) 

A Byzantine node sends K complaints sharing `(transcript_id, dealer_id, complainer_id)` but with distinct `internal_complaint_raw` bytes. Each carries a valid signature (the attacker signs with their own node key) but invalid complaint content. The signature check passes; `verify_complaint` is called K times and fails permanently each time. All K complaints produce `HandleInvalid`; none inserts the key; all K are fully crypto-verified.

The existing test `test_ecdsa_duplicate_complaints_in_batch` only exercises the success path (`CryptoReturningOk`), leaving the failure path untested: [6](#0-5) 

### Impact Explanation
`verify_complaint` is a threshold-crypto operation (involves elliptic-curve pairings over the dealing). Forcing K invocations per batch per key multiplies CPU cost on every honest replica proportionally to K. With no pool-size enforcement visible in this path, a Byzantine node can sustain this amplification across consecutive `on_state_change` rounds, degrading consensus throughput.

### Likelihood Explanation
Any single Byzantine subnet node below the fault threshold can execute this without coordination. The node only needs its own signing key (already held) and the ability to craft complaints with differing `internal_complaint_raw` bytes for the same `(transcript_id, dealer_id)`. No privileged access, governance action, or threshold collusion is required.

### Recommendation
Insert the key into `validated_complaints` unconditionally after calling `crypto_verify_complaint`, regardless of the outcome:

```rust
let action = self.crypto_verify_complaint(id, transcript, signed_complaint);
// Insert key for ALL outcomes, not just MoveToValidated
validated_complaints.insert(key);
ret.append(&mut action.into_iter().collect());
```

This limits crypto work to one invocation per `ComplaintKey` per batch. Transient failures are still retried in the next `on_state_change` round (the artifact remains in the unvalidated pool), so liveness is preserved.

Add a test that injects two same-key complaints with a crypto mock that returns a transient or permanent error for the first, and asserts `crypto_verify_complaint` is called exactly once.

### Proof of Concept
1. Construct two `SignedIDkgComplaint` values sharing `(transcript_id, dealer_id, complainer_id)` but with different `internal_complaint_raw` bytes (nonces 0 and 1), both signed with the Byzantine node's key.
2. Insert both into the unvalidated pool.
3. Configure a mock crypto that records call counts and returns a permanent error for `verify_complaint`.
4. Call `validate_complaints`.
5. Assert `crypto_verify_complaint` was called **twice** — demonstrating the invariant violation.
6. Apply the fix (unconditional key insertion) and assert the call count drops to **one**.

### Citations

**File:** rs/consensus/idkg/src/complaints.rs (L118-118)
```rust
        let mut validated_complaints = BTreeSet::new();
```

**File:** rs/consensus/idkg/src/complaints.rs (L149-151)
```rust
                        if let Some(IDkgChangeAction::MoveToValidated(_)) = action {
                            validated_complaints.insert(key);
                        }
```

**File:** rs/consensus/idkg/src/complaints.rs (L391-412)
```rust
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
```

**File:** rs/consensus/idkg/src/complaints.rs (L420-441)
```rust
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
```

**File:** rs/consensus/idkg/src/complaints.rs (L590-592)
```rust
        idkg_pool
            .validated()
            .complaints_by_prefix(prefix)
```

**File:** rs/consensus/idkg/src/complaints.rs (L1459-1506)
```rust
    fn test_ecdsa_duplicate_complaints_in_batch() {
        ic_test_utilities::artifact_pool_config::with_test_pool_config(|pool_config| {
            with_test_replica_logger(|logger| {
                let key_id = fake_ecdsa_idkg_master_public_key_id();
                let (mut idkg_pool, complaint_handler) =
                    create_complaint_dependencies(pool_config, logger);
                let id_1 = create_transcript_id_with_height(1, Height::from(30));

                // Set up the IDKG pool
                // Complaint from NODE_3 for transcript id_1, dealer NODE_2
                let complaint = create_complaint_with_nonce(id_1, NODE_2, NODE_3, 0);
                let msg_id_1 = complaint.message_id();
                idkg_pool.insert(UnvalidatedArtifact {
                    message: IDkgMessage::Complaint(complaint),
                    peer_id: NODE_3,
                    timestamp: UNIX_EPOCH,
                });

                // Complaint from NODE_3 for transcript id_1, dealer NODE_2
                let complaint = create_complaint_with_nonce(id_1, NODE_2, NODE_3, 1);
                let msg_id_2 = complaint.message_id();
                idkg_pool.insert(UnvalidatedArtifact {
                    message: IDkgMessage::Complaint(complaint),
                    peer_id: NODE_3,
                    timestamp: UNIX_EPOCH,
                });

                let block_reader = TestIDkgBlockReader::for_complainer_test(
                    &key_id,
                    Height::new(100),
                    vec![TranscriptRef::new(Height::new(30), id_1)],
                );
                let snapshot = fake_state_with_signature_requests(Height::from(0), []);
                let active_transcripts =
                    complaint_handler.active_transcripts(&block_reader, &snapshot);
                let change_set = complaint_handler.validate_complaints(
                    &idkg_pool,
                    &block_reader,
                    &active_transcripts,
                );
                assert_eq!(change_set.len(), 2);
                // One is considered duplicate
                assert!(is_removed_from_unvalidated(&change_set, &msg_id_1));
                // One is considered valid
                assert!(is_moved_to_validated(&change_set, &msg_id_2));
            })
        })
    }
```
