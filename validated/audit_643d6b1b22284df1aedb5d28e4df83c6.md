Audit Report

## Title
Byzantine `content_size` Inflation in `ResponsesTooLarge` Bypasses Size Guard — (`rs/https_outcalls/consensus/src/payload_builder.rs`)

## Summary

The `ResponsesTooLarge` branch of `validate_canister_http_payload_impl` uses `share.content.content_size()` directly from signed metadata to compute whether responses are too large, without verifying that value against actual response content. A Byzantine committee member can sign a receipt with an arbitrarily inflated `content_size`, and a Byzantine block proposer can embed that share in a `ResponsesTooLarge` payload. The validator accepts it, causing a valid flexible HTTP request to be permanently and incorrectly resolved as `ResponsesTooLarge`, burning the canister's cycles and denying service.

## Finding Description

`CanisterHttpResponseShare` is defined as `BasicSigned<CanisterHttpResponseReceipt>`, where `CanisterHttpResponseReceipt` contains `CanisterHttpResponseMetadata` including the `content_size` field. [1](#0-0) 

`FlexibleCanisterHttpError::ResponsesTooLarge` carries only raw `Vec<CanisterHttpResponseShare>` — shares without accompanying full responses — unlike `TooManyRejects` which carries `Vec<FlexibleCanisterHttpResponseWithProof>`. [2](#0-1) 

The `ResponsesTooLarge` branch calls `validate_response_share` for each share, which checks only: refund allowance, callback ID match, no duplicate signers, committee membership, and registry version. It does **not** check that `content_size` corresponds to any actual response content. [3](#0-2) 

After validation, the size calculation uses `share.content.content_size() as usize` directly from the signed metadata: [4](#0-3) 

The `smallest_sum` check at line 812 then uses these inflated values to decide whether the payload is valid: [5](#0-4) 

By contrast, `validate_flexible_response_with_proof` (used for `TooManyRejects`) explicitly cross-checks `content_size` and `content_hash` against actual response content: [6](#0-5) 

The pool manager does validate `content_size` against actual response content for shares entering the pool, but this is irrelevant — a Byzantine block proposer constructs `ResponsesTooLarge` payloads directly, bypassing pool ingestion entirely. [7](#0-6) 

The deferred batch signature verification only confirms the Byzantine node's signature is cryptographically valid over the inflated receipt; it does not constrain what `content_size` value was signed. [8](#0-7) 

## Impact Explanation

A Byzantine block proposer can cause any flexible HTTP outcall request to be permanently and incorrectly resolved as `ResponsesTooLarge`, even when the actual responses would fit within `MAX_CANISTER_HTTP_PAYLOAD_SIZE` (2 MiB). The canister loses cycles and is denied service for that request with no recourse. This constitutes application/platform-level DoS on the HTTP outcalls subsystem for targeted canisters, matching the High impact class: "Application/platform-level DoS, crash, consensus blocking, certified-state disruption, or subnet availability impact not based on raw volumetric DDoS." Given the constraints (requires coordinated Byzantine committee member + block proposer), severity is at the lower end of High or upper end of Medium.

## Likelihood Explanation

The attack requires: (a) at least `min_known_ok_needed` Byzantine nodes in the `flex_committee` for the targeted request, and (b) a Byzantine block proposer. `min_known_ok_needed = min_responses - num_unseen`; with `min_responses = 1` or with unseen slots, a single Byzantine committee member suffices. The `flex_committee` is a small per-request subset, making Byzantine membership plausible. Both conditions are below the standard Byzantine fault threshold (f = ⌊(n−1)/3⌋). The attack is repeatable against any flexible HTTP outcall request and requires no victim action.

## Recommendation

In the `ResponsesTooLarge` branch, do not trust `share.content.content_size()` for the size calculation. Either:

1. Require each non-reject share in `all_seen_shares` to be accompanied by its full response (as `FlexibleCanisterHttpResponseWithProof` does for `TooManyRejects`), and compute the size from the actual response content after verifying the content hash and size match — mirroring the check already present in `validate_flexible_response_with_proof`. [6](#0-5) 

2. Alternatively, cap `content_size` at `MAX_CANISTER_HTTP_PAYLOAD_SIZE` during validation so that an inflated value cannot push `smallest_sum` above the limit unless the actual response would also exceed it.

## Proof of Concept

```
State-machine test sketch:
1. Create a Flexible request with committee = {node_0, node_1},
   min_responses = 2.
2. Actual response is 100 bytes.
3. Byzantine node_0 signs CanisterHttpResponseReceipt {
       content_size: MAX_CANISTER_HTTP_PAYLOAD_SIZE as u32 + 1,
       content_hash: <any valid hash>,
       is_reject: false, ...
   }
4. Byzantine node_1 signs the same inflated receipt.
5. Byzantine proposer builds:
   FlexibleCanisterHttpError::ResponsesTooLarge {
       callback_id,
       all_seen_shares: [share_0, share_1],
       total_requests: 2,
       min_responses: 2,
   }
6. Call validate_canister_http_payload_impl with this payload.
7. Assert: result is Ok(()) — payload accepted despite actual responses fitting.
8. Assert: canister receives ResponsesTooLarge error and cycles are burned.
```

`validate_response_share` passes for both shares (committee members, valid callback ID, registry version). Signature verification passes (Byzantine nodes genuinely signed the inflated receipts). `ok_entry_sizes` = [inflated, inflated]; `smallest_sum >> MAX_CANISTER_HTTP_PAYLOAD_SIZE` → `FlexibleResponsesNotTooLarge` check at line 812 is NOT triggered → block accepted. [9](#0-8)

### Citations

**File:** rs/types/types/src/canister_http.rs (L1176-1177)
```rust
/// A signature share of [`CanisterHttpResponseReceipt`].
pub type CanisterHttpResponseShare = BasicSigned<CanisterHttpResponseReceipt>;
```

**File:** rs/types/types/src/batch/canister_http.rs (L48-57)
```rust
    ResponsesTooLarge {
        callback_id: CallbackId,
        all_seen_shares: Vec<CanisterHttpResponseShare>,
        total_requests: u32,
        min_responses: u32,
    },
    TooManyRejects {
        callback_id: CallbackId,
        reject_responses: Vec<FlexibleCanisterHttpResponseWithProof>,
    },
```

**File:** rs/https_outcalls/consensus/src/payload_builder/utils.rs (L173-187)
```rust
    let calculated_hash = crypto_hash(&response_with_proof.response);
    if &calculated_hash != response_with_proof.proof.content.content_hash() {
        return Err(InvalidCanisterHttpPayloadReason::ContentHashMismatch {
            metadata_hash: response_with_proof.proof.content.content_hash().clone(),
            calculated_hash,
        });
    }

    let calculated_size = response_with_proof.response.content.count_bytes() as u32;
    if calculated_size != response_with_proof.proof.content.content_size() {
        return Err(InvalidCanisterHttpPayloadReason::ContentSizeMismatch {
            metadata_size: response_with_proof.proof.content.content_size(),
            calculated_size,
        });
    }
```

**File:** rs/https_outcalls/consensus/src/payload_builder/utils.rs (L208-251)
```rust
pub(crate) fn validate_response_share(
    share: &CanisterHttpResponseShare,
    callback_id: CallbackId,
    flex_committee: &BTreeSet<NodeId>,
    seen_signers: &mut HashSet<NodeId>,
    consensus_registry_version: RegistryVersion,
    per_replica_allowance: Cycles,
) -> Result<(), InvalidCanisterHttpPayloadReason> {
    check_refund_allowance(&share.content.payment_receipt, per_replica_allowance)?;

    if share.content.id() != callback_id {
        return Err(
            InvalidCanisterHttpPayloadReason::FlexibleCallbackIdMismatch {
                callback_id,
                mismatched_id: share.content.id(),
            },
        );
    }

    let signer = share.signature.signer;
    if !seen_signers.insert(signer) {
        return Err(InvalidCanisterHttpPayloadReason::FlexibleDuplicateSigner {
            callback_id,
            signer,
        });
    }
    if !flex_committee.contains(&signer) {
        return Err(
            InvalidCanisterHttpPayloadReason::FlexibleSignerNotInCommittee {
                callback_id,
                signer,
            },
        );
    }

    if share.content.registry_version() != consensus_registry_version {
        return Err(InvalidCanisterHttpPayloadReason::RegistryVersionMismatch {
            expected: consensus_registry_version,
            received: share.content.registry_version(),
        });
    }

    Ok(())
}
```

**File:** rs/https_outcalls/consensus/src/payload_builder.rs (L771-818)
```rust
                    for share in all_seen_shares {
                        validate_response_share(
                            share,
                            callback_id,
                            flex_committee,
                            &mut seen_signers,
                            consensus_registry_version,
                            context.refund_status.per_replica_allowance,
                        )
                        .map_err(CanisterHttpPayloadValidationError::InvalidArtifact)?;
                    }

                    // Defer signature verification.
                    sig_inputs.extend(response_share_sig_inputs(all_seen_shares));

                    let num_unseen = flex_committee.len().saturating_sub(all_seen_shares.len());
                    let min_known_ok_needed = min_responses.saturating_sub(num_unseen);

                    let mut ok_entry_sizes: Vec<usize> = all_seen_shares
                        .iter()
                        .filter(|share| !share.content.is_reject())
                        .map(|share| {
                            FlexibleCanisterHttpResponseWithProof::count_bytes_from_parts(
                                &context.request.sender,
                                share.content.content_size() as usize,
                                share,
                            )
                        })
                        .collect();
                    if ok_entry_sizes.len() < min_known_ok_needed {
                        return invalid_artifact(
                            InvalidCanisterHttpPayloadReason::FlexibleResponsesTooLargeInsufficientEvidence {
                                callback_id,
                                ok_count: ok_entry_sizes.len(),
                                min_known_ok_needed,
                            },
                        );
                    }

                    ok_entry_sizes.sort_unstable();
                    let smallest_sum: usize = ok_entry_sizes.iter().take(min_known_ok_needed).sum();
                    if smallest_sum <= MAX_CANISTER_HTTP_PAYLOAD_SIZE {
                        return invalid_artifact(
                            InvalidCanisterHttpPayloadReason::FlexibleResponsesNotTooLarge(
                                callback_id,
                            ),
                        );
                    }
```

**File:** rs/https_outcalls/consensus/src/payload_builder.rs (L823-832)
```rust
        // Batch-verify the signatures of the deferred shares.
        if !sig_inputs.is_empty() {
            self.crypto
                .verify_basic_sig_batch_multi_msg(&sig_inputs, consensus_registry_version)
                .map_err(|err| {
                    CanisterHttpPayloadValidationError::InvalidArtifact(
                        InvalidCanisterHttpPayloadReason::SignatureError(Box::new(err)),
                    )
                })?;
        }
```

**File:** rs/https_outcalls/consensus/src/pool_manager.rs (L544-549)
```rust
                        if share.content.content_size() != response.content.count_bytes() as u32 {
                            return Some(CanisterHttpChangeAction::HandleInvalid(
                                share.clone(),
                                "Content size does not match the response".to_string(),
                            ));
                        }
```
