Audit Report

## Title
Missing Per-Share `registry_version` Check in Divergence Proof Validation Allows Byzantine Block Proposer to Force Spurious Reject — (`rs/https_outcalls/consensus/src/payload_builder.rs`)

## Summary
`validate_canister_http_payload_impl` enforces a `registry_version` match for `payload.responses` (line 452) and for every share in the flexible path via `validate_response_share` (utils.rs line 243), but the divergence-response loop (lines 538–593) contains no equivalent check. Because `group_shares_by_callback_id` keys its inner map on the full `CanisterHttpResponseMetadata` — which includes `registry_version` — shares for the same HTTP response but carrying different registry versions land in separate groups. A Byzantine block proposer can exploit this during any routine registry-version transition to manufacture artificial divergence and force a spurious `SysTransient` reject for a request that actually had consensus.

## Finding Description
**Regular responses** — guard present at line 452:
```rust
if response.proof.registry_version() != consensus_registry_version {
    return invalid_artifact(RegistryVersionMismatch { … });
}
``` [1](#0-0) 

**Flexible shares** — guard present in `validate_response_share` at utils.rs line 243:
```rust
if share.content.registry_version() != consensus_registry_version {
    return Err(InvalidCanisterHttpPayloadReason::RegistryVersionMismatch { … });
}
``` [2](#0-1) 

**Divergence loop** — no such check anywhere in lines 538–593: [3](#0-2) 

**Grouping key is the full metadata** (including `registry_version`):
```rust
map.entry(share.content.id())
    .or_default()
    .entry(share.content.metadata.clone())   // registry_version is part of the key
    .or_default()
    .push(share);
``` [4](#0-3) 

`CanisterHttpResponseMetadata` derives `Ord`/`PartialOrd` and includes `registry_version` as a field: [5](#0-4) 

Therefore two shares for the **same HTTP response** (identical `content_hash`) but with `registry_version=V` vs `registry_version=V+1` are placed in two distinct groups by `group_shares_by_callback_id`, and `grouped_shares_meet_divergence_criteria` counts them as genuinely diverging responses. [6](#0-5) 

**The build path already filters by registry version**, but the validator does not: [7](#0-6) 

**Batch signature verification** uses `consensus_registry_version` only for public-key lookup, not to enforce that the signed message's embedded `registry_version` matches: [8](#0-7) 

The actual verification is a raw Ed25519 batch check of message bytes against the looked-up key. Because node signing keys are stable across most registry updates, a share signed at V+1 (whose serialized bytes contain `registry_version=V+1`) verifies correctly when the verifier looks up the key at V. The signature check therefore does **not** close the gap. [9](#0-8) 

## Impact Explanation
A single Byzantine block proposer (one compromised node, within the f-fault tolerance budget) can force honest nodes to accept a `CanisterHttpResponseDivergence` block section for a callback that actually reached consensus, causing the canister to receive a spurious `SysTransient` reject instead of the real HTTP response. This is a concrete application/platform-level disruption of the HTTP outcalls subsystem — a production in-scope IC protocol component — with direct user harm (canisters silently lose HTTP outcall results). This matches the **High** impact class: "Application/platform-level DoS, crash, consensus blocking, certified-state disruption, or subnet availability impact not based on raw volumetric DDoS."

## Likelihood Explanation
Registry version transitions are routine on the IC (node additions, parameter changes, etc.). During a transition window, honest nodes legitimately produce shares at both V and V+1, and a Byzantine block proposer receives both sets through normal gossip. Node signing keys are stable across most registry updates, so the signature check does not block the attack. Only one compromised block-proposer slot is needed — well within the f-fault budget. The attack is repeatable on every registry transition for as long as the bug is present.

## Recommendation
Add a per-share registry-version check inside the divergence-response validation loop, before `sig_inputs.extend(…)` and `group_shares_by_callback_id(…)`, mirroring the check already present in `validate_response_share`:

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

This should be inserted at approximately line 552 in `rs/https_outcalls/consensus/src/payload_builder.rs`, immediately after the `invalid_signers` check and before `sig_inputs.extend(response_share_sig_inputs(&response.shares))`. [10](#0-9) 

## Proof of Concept
```
Setup: subnet of 7 nodes (f=2), consensus_registry_version = V.
       Registry transitions to V+1 mid-flight.

Shares collected by Byzantine block proposer:
  share_0: node_0, callback_id=42, content_hash=H, registry_version=V
  share_1: node_1, callback_id=42, content_hash=H, registry_version=V
  share_2: node_2, callback_id=42, content_hash=H, registry_version=V
  share_3: node_3, callback_id=42, content_hash=H, registry_version=V+1
  share_4: node_4, callback_id=42, content_hash=H, registry_version=V+1
  share_5: node_5, callback_id=42, content_hash=H, registry_version=V+1

All nodes are in the committee at V. Keys unchanged between V and V+1.

Crafted payload:
  CanisterHttpResponseDivergence { shares: [share_0..share_5] }

Validation trace:
  1. committee check: all nodes ∈ committee → OK
  2. sig verification at V: all 6 signatures valid (same keys) → OK
  3. group_shares_by_callback_id:
       group { metadata{id=42, hash=H, rv=V}   } → [share_0, share_1, share_2]
       group { metadata{id=42, hash=H, rv=V+1} } → [share_3, share_4, share_5]
  4. grouped_shares_meet_divergence_criteria:
       largest group = 3 signers, non-largest = 3 signers
       otherwise_committed_signer_count = 3 > faults_tolerated = 2 → TRUE
  → divergence criteria met; spurious SysTransient reject delivered to canister.

Reproducible as a unit test in rs/https_outcalls/consensus/src/payload_builder/tests.rs
by constructing two sets of CanisterHttpResponseShare with identical content_hash but
differing registry_version, assembling them into a CanisterHttpResponseDivergence,
and asserting that validate_canister_http_payload returns Ok(()) (demonstrating the
missing rejection).
```

### Citations

**File:** rs/https_outcalls/consensus/src/payload_builder.rs (L212-213)
```rust
                // Filter out shares with the wrong registry version
                .filter(|&share| share.content.registry_version() == consensus_registry_version)
```

**File:** rs/https_outcalls/consensus/src/payload_builder.rs (L452-459)
```rust
            if response.proof.registry_version() != consensus_registry_version {
                return invalid_artifact(
                    InvalidCanisterHttpPayloadReason::RegistryVersionMismatch {
                        expected: consensus_registry_version,
                        received: response.proof.registry_version(),
                    },
                );
            }
```

**File:** rs/https_outcalls/consensus/src/payload_builder.rs (L538-594)
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
        }
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

**File:** rs/crypto/src/sign/basic_sig.rs (L120-122)
```rust
        for (signer, signature, msg_bytes) in inputs {
            let pk_proto =
                key_from_registry(registry, signer, KeyPurpose::NodeSigning, registry_version)?;
```

**File:** rs/crypto/src/sign/basic_sig.rs (L145-154)
```rust
        let seed: [u8; 32] = csprng.write().r#gen();

        ic_ed25519::PublicKey::batch_verify_with_seed(&msgs, &sigs, &keys, &seed).map_err(|e| {
            CryptoError::SignatureVerification {
                algorithm: AlgorithmId::Ed25519,
                public_key_bytes: vec![],
                sig_bytes: vec![],
                internal_error: e.to_string(),
            }
        })
```
