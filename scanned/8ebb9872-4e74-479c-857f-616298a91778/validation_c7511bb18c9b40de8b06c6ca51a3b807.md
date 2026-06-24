The code is confirmed. Let me now assess the full exploit chain carefully.

### Title
Complaint Suppression via `Failure` Short-Circuit in `load_transcripts` — (`rs/consensus/idkg/src/utils.rs`)

### Summary

The `load_transcripts` helper function contains a logic error: when any transcript returns `TranscriptLoadStatus::Failure`, it immediately returns `Some(Default::default())` — an empty change set — discarding all complaints accumulated from transcripts processed earlier in the same loop iteration. A Byzantine dealer who can arrange for one transcript to be in the "complained-but-insufficient-openings" (`Failure`) state while simultaneously corrupting other transcripts can suppress the honest node's complaint broadcasts for those other transcripts for the duration of the `Failure` window.

---

### Finding Description

**Root cause — `rs/consensus/idkg/src/utils.rs` lines 227–246:**

```rust
pub(super) fn load_transcripts(...) -> Option<IDkgChangeSet> {
    let mut new_complaints = Vec::new();
    for transcript in transcripts {
        match transcript_loader.load_transcript(idkg_pool, transcript) {
            TranscriptLoadStatus::Success => (),
            TranscriptLoadStatus::Failure => return Some(Default::default()), // ← drops new_complaints
            TranscriptLoadStatus::Complaints(complaints) => {
                for complaint in complaints {
                    new_complaints.push(...);
                }
            }
        }
    }
    if new_complaints.is_empty() { None } else { Some(new_complaints) }
}
``` [1](#0-0) 

The `return Some(Default::default())` on the `Failure` arm exits the function immediately, silently dropping every `IDkgChangeAction::AddToValidated(Complaint(...))` that was pushed into `new_complaints` by earlier loop iterations.

**How `Failure` arises — `rs/consensus/idkg/src/complaints.rs` lines 877–955:**

`TranscriptLoadStatus::Failure` is returned in three situations:
1. `IDkgProtocol::load_transcript` returns any `Err` (e.g., `SerializationError`, `PrivateKeyNotFound`). [2](#0-1) 
2. `crypto_create_complaint` fails (transient signing error). [3](#0-2) 
3. The node has already complained about the transcript but `load_transcript_with_openings` returns `InsufficientOpenings` — the normal state while waiting for peers to supply openings. [4](#0-3) 

Case 3 is the most realistic trigger: it is the documented, expected intermediate state after a node has complained but before enough openings arrive.

**Callers that pass multi-transcript slices:**

- ECDSA signing path passes **5 transcripts** (`kappa_unmasked`, `lambda_masked`, `kappa_times_lambda`, `key_times_lambda`, `key_transcript`). [5](#0-4) 
- Pre-signer `UnmaskedTimesMasked` passes **2 transcripts**. [6](#0-5) 

---

### Impact Explanation

When the bug fires, the honest node's `on_state_change` loop returns an empty change set instead of the accumulated complaints. Those complaints are never added to the validated pool and therefore never gossiped to peers. Without the complaints:

- Other nodes do not generate openings for the affected transcripts.
- The affected transcripts cannot be reconstructed via the complaint/opening sub-protocol.
- Signature shares that depend on those transcripts cannot be produced.

The impact is **complaint suppression for the duration of the `Failure` window**. Because `Failure` (case 3) resolves once enough openings accumulate for the already-complained transcript, the suppression is **temporary** rather than permanent under normal Byzantine assumptions (f < n/3). Once the `Failure` transcript transitions to `Success`, the next call to `load_transcripts` will correctly return the complaints for the other transcripts. However, the window can be extended by a Byzantine dealer who simultaneously corrupts multiple transcripts, staggering the corruption so that at least one transcript is always in the `Failure` state, continuously suppressing new complaints.

---

### Likelihood Explanation

- A single Byzantine dealer (below the fault threshold) can provide corrupted MEGa-encrypted dealings targeted at a specific receiver, causing that receiver to generate complaints.
- The dealer can corrupt transcript A in round R (causing the receiver to complain), then corrupt transcripts B and C in round R+1. During the window in which A is in `Failure` (insufficient openings), complaints for B and C are suppressed.
- This requires no privileged access, no key material, and no majority corruption — only a single malicious dealer node and knowledge of the transcript ordering in `load_transcripts`.

---

### Recommendation

Replace the early-return-with-empty-set on `Failure` with behavior that either:

1. **Propagates the failure without discarding complaints** — return `Some(new_complaints)` if any complaints were accumulated, regardless of the `Failure`:

```rust
TranscriptLoadStatus::Failure => {
    if new_complaints.is_empty() {
        return Some(Default::default());
    } else {
        return Some(new_complaints);
    }
}
```

2. Or treat `Failure` as a signal to stop processing but still emit accumulated complaints, so the caller receives both the complaints and an indication that loading was incomplete.

The fix ensures that complaints accumulated before a `Failure` are never silently discarded.

---

### Proof of Concept

A unit test using the existing `TestIDkgTranscriptLoader` mock infrastructure (already present in `rs/consensus/idkg/src/test_utils.rs`) [7](#0-6)  would need a per-transcript configurable loader returning `[Complaints, Complaints, Failure]`. The assertion `assert!(change_set.is_empty())` would pass today — confirming the bug — while the correct behavior requires the change set to contain the two complaints from the first two transcripts.

### Citations

**File:** rs/consensus/idkg/src/utils.rs (L227-246)
```rust
    let mut new_complaints = Vec::new();
    for transcript in transcripts {
        match transcript_loader.load_transcript(idkg_pool, transcript) {
            TranscriptLoadStatus::Success => (),
            TranscriptLoadStatus::Failure => return Some(Default::default()),
            TranscriptLoadStatus::Complaints(complaints) => {
                for complaint in complaints {
                    new_complaints.push(IDkgChangeAction::AddToValidated(IDkgMessage::Complaint(
                        complaint,
                    )));
                }
            }
        }
    }

    if new_complaints.is_empty() {
        None
    } else {
        Some(new_complaints)
    }
```

**File:** rs/consensus/idkg/src/complaints.rs (L892-902)
```rust
            Err(err) => {
                warn!(
                    every_n_seconds => 10,
                    self.log,
                    "Failed to load transcript: transcript_id: {:?}, error = {:?}",
                    transcript.transcript_id,
                    err
                );
                self.metrics.complaint_errors_inc("load_transcript");
                return TranscriptLoadStatus::Failure;
            }
```

**File:** rs/consensus/idkg/src/complaints.rs (L914-916)
```rust
                } else {
                    return TranscriptLoadStatus::Failure;
                }
```

**File:** rs/consensus/idkg/src/complaints.rs (L938-941)
```rust
            Err(IDkgLoadTranscriptError::InsufficientOpenings { .. }) => {
                self.metrics
                    .complaint_errors_inc("load_transcript_with_openings_threshold");
                TranscriptLoadStatus::Failure
```

**File:** rs/consensus/idkg/src/signer.rs (L378-392)
```rust
            ThresholdSigInputs::Ecdsa(inputs) => vec![
                inputs.presig_quadruple().kappa_unmasked(),
                inputs.presig_quadruple().lambda_masked(),
                inputs.presig_quadruple().kappa_times_lambda(),
                inputs.presig_quadruple().key_times_lambda(),
                inputs.key_transcript(),
            ],
            ThresholdSigInputs::Schnorr(inputs) => vec![
                inputs.presig_transcript().blinder_unmasked(),
                inputs.key_transcript(),
            ],
            // No dependencies for VetKd
            ThresholdSigInputs::VetKd(_) => vec![],
        };
        load_transcripts(idkg_pool, transcript_loader, &transcripts)
```

**File:** rs/consensus/idkg/src/pre_signer.rs (L1076-1078)
```rust
            IDkgTranscriptOperation::UnmaskedTimesMasked(t1, t2) => {
                load_transcripts(idkg_pool, transcript_loader, &[t1, t2])
            }
```

**File:** rs/consensus/idkg/src/test_utils.rs (L254-301)
```rust
pub(crate) enum TestTranscriptLoadStatus {
    Success,
    Failure,
    Complaints,
}

pub(crate) struct TestIDkgTranscriptLoader {
    load_transcript_result: TestTranscriptLoadStatus,
    returned_complaints: Mutex<Vec<SignedIDkgComplaint>>,
}

impl TestIDkgTranscriptLoader {
    pub(crate) fn new(load_transcript_result: TestTranscriptLoadStatus) -> Self {
        Self {
            load_transcript_result,
            returned_complaints: Mutex::new(Vec::new()),
        }
    }

    pub(crate) fn returned_complaints(&self) -> Vec<SignedIDkgComplaint> {
        let complaints = self.returned_complaints.lock().unwrap();
        let mut ret = Vec::new();
        for complaint in complaints.iter() {
            ret.push(complaint.clone());
        }
        ret
    }
}

impl IDkgTranscriptLoader for TestIDkgTranscriptLoader {
    fn load_transcript(
        &self,
        _idkg_pool: &dyn IDkgPool,
        transcript: &IDkgTranscript,
    ) -> TranscriptLoadStatus {
        match self.load_transcript_result {
            TestTranscriptLoadStatus::Success => TranscriptLoadStatus::Success,
            TestTranscriptLoadStatus::Failure => TranscriptLoadStatus::Failure,
            TestTranscriptLoadStatus::Complaints => {
                let complaint = create_complaint(transcript.transcript_id, NODE_1, NODE_1);
                self.returned_complaints
                    .lock()
                    .unwrap()
                    .push(complaint.clone());
                TranscriptLoadStatus::Complaints(vec![complaint])
            }
        }
    }
```
