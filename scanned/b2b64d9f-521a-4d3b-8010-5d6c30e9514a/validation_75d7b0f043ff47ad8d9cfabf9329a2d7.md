Now I have enough information to make a definitive determination. Let me trace the exact code paths.

### Title
Missing Per-Share `registry_version` Check in Divergence Proof Validation Allows Byzantine Block Proposer to Force Spurious Reject — (`rs/https_outcalls/consensus/src/payload_builder.rs`)

---

### Summary

`validate_canister_http_payload_impl` explicitly checks `registry_version` for `payload.responses` and for every share in flexible paths, but performs **no such check** for shares inside `payload.divergence_responses`. Because `group_shares_by_callback_id` keys the inner map on the full `CanisterHttpResponseMetadata` (which includes `registry_version`), shares at version V and V+1 that carry the **same content hash** land in **separate groups**. A Byzantine block proposer can exploit this to manufacture artificial divergence during a registry-version transition, causing honest nodes to accept a spurious reject delivery for a request that actually had consensus.

---

### Finding Description

**Regular responses** — explicit guard present:

```rust
// payload_builder.rs line 452
if response.proof.registry_version() != consensus_registry_version {
    return invalid_artifact(RegistryVersionMismatch { … });
}
``` [1](#0-0) 

**Flexible shares** — `validate_response_share` has an explicit guard:

```rust
// utils.rs line 243
if share.content.registry_version() != consensus_registry_version {
    return Err(InvalidCanisterHttpPayloadReason::RegistryVersionMismatch { … });
}
``` [2](#0-1) 

**Divergence shares** — the entire validation block (lines 538–593) contains **no registry-version check**:

```rust
for response in &payload.divergence_responses {
    let (valid_signers, invalid_signers) = response.shares.iter()
        .map(|share| share.signature.signer)
        .partition(|signer| committee.iter().any(|id| id == signer));
    // ← no check: share.content.metadata.registry_version == consensus_registry_version
    sig_inputs.extend(response_share_sig_inputs(&response.shares));
    let grouped_shares = group_shares_by_callback_id(response.shares.iter());
    …
    if !grouped_shares_meet_divergence_criteria(&grouped_shares, faults_tolerated) { … }
}
``` [3](#0-2) 

**Grouping key is the full metadata** (including `registry_version`):

```rust
map.entry(share.content.id())
    .or_default()
    .entry(share.content.metadata.clone())   // ← registry_version is part of the key
    .or_default()
    .push(share);
``` [4](#0-3) 

`CanisterHttpResponseMetadata` derives `Ord`/`PartialOrd` and includes `registry_version` as a field: [5](#0-4) 

So two shares for the **same HTTP response** (identical `content_hash`) but with `registry_version=V` vs `registry_version=V+1` are placed in **two distinct groups** by `group_shares_by_callback_id`, and `grouped_shares_meet_divergence_criteria` counts them as genuinely diverging responses. [6](#0-5) 

**The payload-builder side already filters by registry version**, but the validator does not:

```rust
// payload_builder.rs line 213 — build path only
.filter(|&share| share.content.registry_version() == consensus_registry_version)
``` [7](#0-6) 

**Batch signature verification** uses `consensus_registry_version` only for public-key lookup, not to enforce that the signed message's embedded `registry_version` matches:

```rust
self.crypto.verify_basic_sig_batch_multi_msg(&sig_inputs, consensus_registry_version)
``` [8](#0-7) 

Because node signing keys rarely change between consecutive registry versions, a share signed at V+1 (whose message bytes contain `registry_version=V+1`) will still verify correctly when the verifier looks up the key at V. The signature check therefore does **not** close the gap.

---

### Impact Explanation

A Byzantine block proposer (a single compromised subnet node, within the f-fault tolerance model) can:

1. Collect honest shares at registry version V (signed before the transition).
2. Collect honest shares at registry version V+1 (signed after the transition) — both sets are legitimately received from honest peers.
3. Assemble a `CanisterHttpResponseDivergence` mixing both sets for the **same callback ID**.
4. All signers pass the committee check (they are in the committee at `consensus_registry_version`).
5. Signatures verify (keys unchanged between V and V+1).
6. `group_shares_by_callback_id` splits the shares into two groups (different metadata due to different `registry_version`), even though both groups carry the same `content_hash`.
7. `grouped_shares_meet_divergence_criteria` sees enough "diverging" signers and returns `true`.
8. Honest nodes accept the block; the canister receives a spurious `SysTransient` reject for an HTTP outcall that actually had consensus.

---

### Likelihood Explanation

- Registry version transitions are routine on the IC (node additions, parameter changes, etc.).
- During a transition window, honest nodes legitimately produce shares at both V and V+1; a Byzantine block proposer receives both sets through normal gossip.
- Node signing keys are stable across most registry updates, so the signature check does not block the attack.
- Only one compromised block-proposer slot is needed — well within the f-fault budget.

---

### Recommendation

Add a per-share registry-version check inside the divergence-response validation loop, mirroring the check already present in `validate_response_share`:

```rust
for share in &response.shares {
    if share.content.metadata.registry_version != consensus_registry_version {
        return invalid_artifact(InvalidCanisterHttpPayloadReason::RegistryVersionMismatch {
            expected: consensus_registry_version,
            received: share.content.metadata.registry_version,
        });
    }
}
```

This should be inserted before `sig_inputs.extend(…)` and `group_shares_by_callback_id(…)` in the divergence loop. [9](#0-8) 

---

### Proof of Concept

```
Setup: subnet of 4 nodes (f=1), consensus_registry_version = V.
       Registry transitions to V+1 mid-flight.

Shares collected by Byzantine block proposer:
  share_A: node_0, callback_id=42, content_hash=H, registry_version=V
  share_B: node_1, callback_id=42, content_hash=H, registry_version=V+1

Both nodes are in the committee at V. Keys unchanged between V and V+1.

Crafted payload:
  CanisterHttpResponseDivergence { shares: [share_A, share_B] }

Validation trace:
  1. committee check: node_0 ∈ committee, node_1 ∈ committee → OK
  2. sig verification at V: both signatures valid (same keys) → OK
  3. group_shares_by_callback_id:
       group { id=42, content_hash=H, registry_version=V   } → [share_A]
       group { id=42, content_hash=H, registry_version=V+1 } → [share_B]
  4. grouped_shares_meet_divergence_criteria:
       largest group = 1 signer, non-largest = 1 signer
       otherwise_committed_signer_count = 1 > faults_tolerated = 1? → FALSE for f=1

Adjust to subnet of 7 (f=2):
  shares at V:   node_0, node_1, node_2  (3 shares)
  shares at V+1: node_3, node_4, node_5  (3 shares)
  largest group = 3, non-largest = 3, otherwise_committed = 3 > 2 → TRUE
  → divergence criteria met, spurious reject delivered.
```

### Citations

**File:** rs/https_outcalls/consensus/src/payload_builder.rs (L212-213)
```rust
                // Filter out shares with the wrong registry version
                .filter(|&share| share.content.registry_version() == consensus_registry_version)
```

**File:** rs/https_outcalls/consensus/src/payload_builder.rs (L451-459)
```rust
            // Validate response against consensus registry version
            if response.proof.registry_version() != consensus_registry_version {
                return invalid_artifact(
                    InvalidCanisterHttpPayloadReason::RegistryVersionMismatch {
                        expected: consensus_registry_version,
                        received: response.proof.registry_version(),
                    },
                );
            }
```

**File:** rs/https_outcalls/consensus/src/payload_builder.rs (L538-593)
```rust
        for response in &payload.divergence_responses {
            let (valid_signers, invalid_signers): (Vec<NodeId>, Vec<NodeId>) = response
                .shares
                .iter()
                .map(|share| share.signature.signer)
                .partition(|signer| committee.iter().any(|id| id == signer));

            if !invalid_signers.is_empty() {
                return invalid_artifact(InvalidCanisterHttpPayloadReason::SignersNotMembers {
                    invalid_signers,
                    committee,
                    valid_signers,
                });
            }

            // Defer signature verification.
            sig_inputs.extend(response_share_sig_inputs(&response.shares));

            let grouped_shares = group_shares_by_callback_id(response.shares.iter());
            if grouped_shares.len() != 1 {
                return invalid_artifact(
                    InvalidCanisterHttpPayloadReason::DivergenceProofContainsMultipleCallbackIds,
                );
            }
            for (callback_id, grouped_shares) in grouped_shares {
                if !delivered_ids.insert(callback_id) {
                    return invalid_artifact(InvalidCanisterHttpPayloadReason::DuplicateResponse(
                        callback_id,
                    ));
                }
                let context = http_contexts.get(&callback_id).ok_or(
                    CanisterHttpPayloadValidationError::InvalidArtifact(
                        InvalidCanisterHttpPayloadReason::UnknownCallbackId(callback_id),
                    ),
                )?;
                if !matches!(context.replication, Replication::FullyReplicated) {
                    return invalid_artifact(
                        InvalidCanisterHttpPayloadReason::InvalidPayloadSection(callback_id),
                    );
                }

                // Enforce per-replica refund allowance for divergence shares.
                for share in grouped_shares.values().flatten() {
                    utils::check_refund_allowance(
                        &share.content.payment_receipt,
                        context.refund_status.per_replica_allowance,
                    )
                    .map_err(CanisterHttpPayloadValidationError::InvalidArtifact)?;
                }

                if !grouped_shares_meet_divergence_criteria(&grouped_shares, faults_tolerated) {
                    return invalid_artifact(
                        InvalidCanisterHttpPayloadReason::DivergenceProofDoesNotMeetDivergenceCriteria,
                    );
                }
            }
```

**File:** rs/https_outcalls/consensus/src/payload_builder.rs (L824-831)
```rust
        if !sig_inputs.is_empty() {
            self.crypto
                .verify_basic_sig_batch_multi_msg(&sig_inputs, consensus_registry_version)
                .map_err(|err| {
                    CanisterHttpPayloadValidationError::InvalidArtifact(
                        InvalidCanisterHttpPayloadReason::SignatureError(Box::new(err)),
                    )
                })?;
```

**File:** rs/https_outcalls/consensus/src/payload_builder/utils.rs (L243-248)
```rust
    if share.content.registry_version() != consensus_registry_version {
        return Err(InvalidCanisterHttpPayloadReason::RegistryVersionMismatch {
            expected: consensus_registry_version,
            received: share.content.registry_version(),
        });
    }
```

**File:** rs/https_outcalls/consensus/src/payload_builder/utils.rs (L295-317)
```rust
pub(crate) fn grouped_shares_meet_divergence_criteria(
    grouped_shares: &BTreeMap<CanisterHttpResponseMetadata, Vec<&CanisterHttpResponseShare>>,
    faults_tolerated: usize,
) -> bool {
    let mut share_for_content_signers: Vec<BTreeSet<NodeId>> = grouped_shares
        .values()
        .map(|shares| shares.iter().map(|share| share.signature.signer).collect())
        .collect();
    share_for_content_signers.sort_by_key(|b| core::cmp::Reverse(b.len()));
    if let Some(largest_signers) = share_for_content_signers.first() {
        let mut non_largest_signers = BTreeSet::new();
        for signer_group in share_for_content_signers.iter().skip(1) {
            for signer in signer_group.iter() {
                non_largest_signers.insert(*signer);
            }
        }
        let otherwise_committed_signer_count =
            non_largest_signers.difference(largest_signers).count();
        otherwise_committed_signer_count > faults_tolerated
    } else {
        false
    }
}
```

**File:** rs/https_outcalls/consensus/src/payload_builder/utils.rs (L337-343)
```rust
    for share in shares {
        map.entry(share.content.id())
            .or_default()
            .entry(share.content.metadata.clone())
            .or_default()
            .push(share);
    }
```

**File:** rs/types/types/src/canister_http.rs (L1037-1046)
```rust
#[derive(Clone, Eq, PartialEq, Ord, PartialOrd, Hash, Debug, Deserialize, Serialize)]
#[cfg_attr(test, derive(ExhaustiveSet))]
pub struct CanisterHttpResponseMetadata {
    pub id: CallbackId,
    pub content_hash: CryptoHashOf<CanisterHttpResponse>,
    pub content_size: u32,
    pub is_reject: bool,
    pub registry_version: RegistryVersion,
    pub replica_version: ReplicaVersion,
}
```
