The code tells a clear story. Let me lay out the exact finding.

**Two validation functions, two different behaviors:**

`validate_flexible_response_with_proof` (success path) verifies `content_size` against the actual response body: [1](#0-0) 

`validate_response_share` (used in the `ResponsesTooLarge` path) checks only committee membership, registry version, refund allowance, callback ID, and duplicate signers — **no `content_size` vs actual body check**: [2](#0-1) 

The `ResponsesTooLarge` branch calls only `validate_response_share`, then directly trusts `share.content.content_size()` to compute `ok_entry_sizes`: [3](#0-2) 

**The pool manager guard and why it doesn't close the hole:**

The pool manager does check `content_size` against the actual response body when admitting shares: [4](#0-3) 

This prevents inflated shares from entering **honest nodes' pools**. But a Byzantine node acting as block proposer does not construct its payload by reading from the pool manager — it can directly serialize any payload it wants. The pool manager guard is irrelevant to a Byzantine proposer.

**The single-node attack path:**

A single Byzantine node that is simultaneously in the `flex_committee` and is the block proposer for a round can:

1. Sign a `CanisterHttpResponseShare` with `metadata.content_size = MAX_CANISTER_HTTP_PAYLOAD_SIZE + 1` (valid cryptographic signature over inflated metadata — the crypto layer signs whatever the node presents).
2. Directly construct a block payload containing `FlexibleCanisterHttpError::ResponsesTooLarge` with `all_seen_shares` including this share.
3. The validator calls `validate_response_share` → passes (committee member, valid registry version, no duplicate).
4. Signature batch-verification passes (the Byzantine node genuinely signed that metadata).
5. `ok_entry_sizes` is computed from the inflated `content_size` → `smallest_sum > MAX_CANISTER_HTTP_PAYLOAD_SIZE`.
6. The check at line 812 passes → the `ResponsesTooLarge` error is accepted → callback permanently terminated.

The precondition `min_known_ok_needed >= 1` (line 787) must hold, which is satisfied whenever `min_responses > committee.len() - all_seen_shares.len()` — a routine condition once most committee members have submitted shares. [5](#0-4) 

---

### Title
Unverified `content_size` in `ResponsesTooLarge` validation allows a single Byzantine committee member/proposer to permanently terminate a legitimate HTTP outcall — (`rs/https_outcalls/consensus/src/payload_builder.rs`)

### Summary
The `ResponsesTooLarge` error path in `validate_canister_http_payload_impl` trusts the `content_size` field from share metadata without verifying it against an actual response body. A single Byzantine node that is both a flex_committee member and the block proposer can sign a share with an arbitrarily inflated `content_size`, include it in a `ResponsesTooLarge` payload, and cause every honest validator to accept the error — permanently terminating a legitimate HTTP outcall callback.

### Finding Description
`validate_flexible_response_with_proof` (used in the success path) enforces that `content_size` in the share metadata matches the actual serialized response body size (lines 181–187 of `utils.rs`). The `ResponsesTooLarge` path uses only `validate_response_share`, which has no such check. It then computes `ok_entry_sizes` directly from `share.content.content_size()` (line 795 of `payload_builder.rs`) and accepts the error if `smallest_sum > MAX_CANISTER_HTTP_PAYLOAD_SIZE`. The pool manager's `content_size` guard (line 544 of `pool_manager.rs`) only applies to pool admission on honest nodes and is bypassed entirely by a Byzantine proposer constructing a payload directly.

### Impact Explanation
The affected HTTP outcall callback is permanently terminated with a `ResponsesTooLarge` error. The canister loses the response, cycles are consumed without a valid result, and the callback can never be retried (it is finalized in the block). Impact is canister integrity loss and denial of service for the specific outcall.

### Likelihood Explanation
A Byzantine node is in the flex_committee with probability proportional to committee size / subnet size. It will be the block proposer for some round with probability 1/n per round. Since callbacks have finite but non-trivial timeouts, the Byzantine node has multiple opportunities to be the proposer while the callback is pending. No external resources, no threshold corruption, and no collusion beyond a single node are required.

### Recommendation
In the `ResponsesTooLarge` validation branch, require that each non-reject share in `all_seen_shares` is accompanied by its actual response body (as `FlexibleCanisterHttpResponseWithProof`), and call `validate_flexible_response_with_proof` instead of `validate_response_share`. This enforces the same `content_size` ↔ actual body consistency check that the success path already applies. Alternatively, add an explicit check that `share.content.content_size()` does not exceed `MAX_CANISTER_HTTP_PAYLOAD_SIZE` per share before summing, which at minimum prevents a single share from trivially exceeding the limit.

### Proof of Concept
```
1. Byzantine node B is in flex_committee for callback_id C.
2. B signs CanisterHttpResponseShare with:
     metadata.content_size = MAX_CANISTER_HTTP_PAYLOAD_SIZE + 1
     metadata.content_hash = <any valid hash>
     metadata.is_reject = false
   (valid signature — crypto signs whatever metadata B presents)
3. B is the block proposer for round R. B constructs payload:
     FlexibleCanisterHttpError::ResponsesTooLarge {
       all_seen_shares: [share_from_step_2, ...honest_shares...],
       total_requests: committee.len(),
       min_responses: <correct value>,
     }
4. Honest validator receives block:
   - validate_response_share(share_from_step_2) → OK (B is in committee, valid registry version)
   - sig batch verify → OK (B genuinely signed this metadata)
   - ok_entry_sizes = [MAX+1, ...]  ← inflated value trusted directly
   - smallest_sum = MAX+1 > MAX → condition at line 812 passes
   - ResponsesTooLarge accepted → callback C permanently terminated
5. Canister receives ResponsesTooLarge error; cycles consumed; no valid response delivered.
```

### Citations

**File:** rs/https_outcalls/consensus/src/payload_builder/utils.rs (L181-187)
```rust
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

**File:** rs/https_outcalls/consensus/src/pool_manager.rs (L544-549)
```rust
                        if share.content.content_size() != response.content.count_bytes() as u32 {
                            return Some(CanisterHttpChangeAction::HandleInvalid(
                                share.clone(),
                                "Content size does not match the response".to_string(),
                            ));
                        }
```
