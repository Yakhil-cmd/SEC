### Title
Byzantine Flex-Committee Member Can Forge `content_size` in `ResponsesTooLarge` to Permanently Terminate a Legitimate HTTP Outcall — (`rs/https_outcalls/consensus/src/payload_builder.rs`)

---

### Summary

The `ResponsesTooLarge` validation branch in `validate_canister_http_payload_impl` computes `smallest_sum` from `share.content.content_size()` — a self-reported metadata field — without ever verifying it against an actual response body. A Byzantine node that is both a flex-committee member and the block proposer can sign a share with an arbitrarily inflated `content_size`, include it in a crafted `ResponsesTooLarge` error, and cause honest validators to accept a false termination of a legitimate HTTP outcall callback.

---

### Finding Description

**The missing guard in `validate_response_share`**

The `ResponsesTooLarge` branch calls `validate_response_share` for each share in `all_seen_shares`: [1](#0-0) 

`validate_response_share` checks: callback ID, committee membership, duplicate signers, registry version, and refund allowance. It explicitly does **not** check `content_size` against any actual response body: [2](#0-1) 

**Contrast with `validate_flexible_response_with_proof`**, which *does* verify both `content_hash` and `content_size` against the actual response body (lines 173–187): [3](#0-2) 

That stronger function is called for `TooManyRejects` and normal `flexible_responses` paths — but **not** for `ResponsesTooLarge`, because the error carries only `CanisterHttpResponseShare` objects (metadata + signature), not full response bodies: [4](#0-3) 

**`ok_entry_sizes` is computed from the untrusted metadata field** [5](#0-4) 

`share.content.content_size()` is the value the Byzantine node signed into its share metadata. There is no cross-check against a real body anywhere in this path.

**Pool manager validation does not protect the consensus path**

The pool manager does enforce `content_size == response.content.count_bytes()` when admitting artifacts: [6](#0-5) 

However, a Byzantine block proposer constructs the block payload directly. They are not constrained to use only pool-admitted shares; they can embed any validly-signed `CanisterHttpResponseShare` directly into the `ResponsesTooLarge` error in the block they propose.

---

### Concrete Attack Scenario

Setup: `flex_committee = {A (Byzantine), B, C}`, `min_responses = 3` (all must respond OK).

1. Byzantine node A signs a `CanisterHttpResponseShare` with `content_size = MAX_CANISTER_HTTP_PAYLOAD_SIZE + 1`. The signature is cryptographically valid (A signs with its own key).
2. A waits until it is the block proposer (block proposer rotates in IC consensus).
3. A crafts `FlexibleCanisterHttpError::ResponsesTooLarge` with `all_seen_shares = [A's inflated share]`, excluding honest nodes B and C.
4. Validator computes:
   - `num_unseen = 3 - 1 = 2`
   - `min_known_ok_needed = 3 - 2 = 1`
   - `ok_entry_sizes = [MAX + 1]`
   - `smallest_sum = MAX + 1 > MAX` → **accepted** [7](#0-6) 

5. The callback is permanently terminated with a `ResponsesTooLarge` error. Cycles are consumed. The canister receives no valid response.

The precondition `min_responses = flex_committee.len()` is required for `min_known_ok_needed = 1` with a single included share. This is a valid canister-configured value.

---

### Impact Explanation

- Legitimate HTTP outcall permanently blocked; the callback can never be retried once terminated via consensus.
- Cycles consumed without a valid response delivered to the canister.
- The canister has no recourse — the error is indistinguishable from a genuine `ResponsesTooLarge` condition.

---

### Likelihood Explanation

The attack requires the Byzantine node to be **both** a flex-committee member and the block proposer in the same round. Block proposer rotates deterministically in IC consensus, so a Byzantine committee member will eventually hold the proposer role while the outcall is still pending. The outcall remains pending until resolved, giving the attacker a window. No threshold corruption, no key leakage, and no external dependency is required — only a single Byzantine node with a valid signing key.

---

### Recommendation

In the `ResponsesTooLarge` validation branch, do not trust `share.content.content_size()` as the authoritative size. Instead, enforce an upper bound on `content_size` per share equal to `MAX_CANISTER_HTTP_RESPONSE_BYTES` (the same limit enforced by the pool manager). A single share cannot legitimately report a `content_size` larger than the per-response hard cap. Reject any share in `all_seen_shares` whose `content_size` exceeds this bound before computing `ok_entry_sizes`.

Concretely, add a check analogous to:

```rust
// In the ResponsesTooLarge branch, after validate_response_share:
if !share.content.is_reject()
    && share.content.content_size() > MAX_CANISTER_HTTP_RESPONSE_BYTES as u32
{
    return invalid_artifact(...);
}
```

This mirrors the size enforcement already present in the pool manager: [8](#0-7) 

---

### Proof of Concept

The existing test `flexible_error_responses_too_large_valid` (tests.rs line 3779) already demonstrates that the validator accepts a `ResponsesTooLarge` payload built entirely from `metadata_share_with_content_size` — shares with arbitrary `content_size` values and no actual response body present: [9](#0-8) 

A fuzz variant of this test that sets `content_size = MAX_CANISTER_HTTP_PAYLOAD_SIZE + 1` for a single share (with `min_known_ok_needed = 1`) will pass validation, confirming the invariant is broken.

### Citations

**File:** rs/https_outcalls/consensus/src/payload_builder.rs (L771-781)
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
```

**File:** rs/https_outcalls/consensus/src/payload_builder.rs (L786-818)
```rust
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

**File:** rs/types/types/src/batch/canister_http.rs (L48-53)
```rust
    ResponsesTooLarge {
        callback_id: CallbackId,
        all_seen_shares: Vec<CanisterHttpResponseShare>,
        total_requests: u32,
        min_responses: u32,
    },
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

**File:** rs/https_outcalls/consensus/src/pool_manager.rs (L558-573)
```rust
                        // An honest replica enforces that response.content.count_bytes() does not exceed max_response_bytes
                        // when the content is `Success`. However it doesn't enroce anything in the case of `Failure`.
                        // As we still want to set a limit for failure, we enforce 1KB, which is reasonable for
                        // an error message.

                        // for flexible calls, max_response_bytes is always None
                        if let Err(e) = validate_response_size(response, context.max_response_bytes)
                        {
                            return Some(CanisterHttpChangeAction::HandleInvalid(
                                share.clone(),
                                format!(
                                    "Http Response for request ID {} is too large: {}",
                                    response.id, e
                                ),
                            ));
                        }
```

**File:** rs/https_outcalls/consensus/src/payload_builder/tests.rs (L3786-3808)
```rust
    let huge_content_size = (MAX_CANISTER_HTTP_PAYLOAD_SIZE as u32 / 2) + 100_000;
    setup_test_with_flexible_context(num_nodes, callback_id, committee, 2, 4, |pb, _pool| {
        let all_seen_shares: Vec<_> = (0..4)
            .map(|i| metadata_share_with_content_size(callback_id.get(), i, huge_content_size))
            .collect();

        let payload = CanisterHttpPayload {
            flexible_errors: vec![FlexibleCanisterHttpError::ResponsesTooLarge {
                callback_id,
                all_seen_shares,
                total_requests: 4,
                min_responses: 2,
            }],
            ..Default::default()
        };
        let result = pb.validate_payload(
            Height::new(1),
            &test_proposal_context(&default_validation_context()),
            &payload_to_bytes_max_4mb(payload),
            &[],
        );
        assert_matches!(result, Ok(()));
    });
```
