Audit Report

## Title
Byzantine `content_size` Inflation in `ResponsesTooLarge` Bypasses Size Guard — (`rs/https_outcalls/consensus/src/payload_builder.rs`)

## Summary
The `ResponsesTooLarge` branch of `validate_canister_http_payload_impl` computes the size proof using `share.content.content_size()` taken directly from the signed metadata of each `CanisterHttpResponseShare`, without verifying that value against actual response content. A Byzantine committee member can sign a receipt with an arbitrarily inflated `content_size`, and a Byzantine block proposer can embed that share in a `ResponsesTooLarge` payload. All existing checks pass, the block is accepted, and the targeted canister permanently receives a `ResponsesTooLarge` error with its cycles burned.

## Finding Description

`FlexibleCanisterHttpError::ResponsesTooLarge` carries `all_seen_shares: Vec<CanisterHttpResponseShare>` — signed metadata only, no actual response body. [1](#0-0) 

During validation, each share is passed through `validate_response_share`, which checks refund allowance, callback-ID match, duplicate signers, committee membership, and registry version — but performs no check that `content_size` corresponds to any actual response content. [2](#0-1) 

After that check passes, the size proof is computed by feeding `share.content.content_size()` directly into `FlexibleCanisterHttpResponseWithProof::count_bytes_from_parts`: [3](#0-2) 

`count_bytes_from_parts` accepts `content_size` as a plain `usize` parameter and uses it without further validation: [4](#0-3) 

The resulting `smallest_sum` is compared against `MAX_CANISTER_HTTP_PAYLOAD_SIZE` to decide whether the `ResponsesTooLarge` claim is legitimate: [5](#0-4) 

By contrast, `validate_flexible_response_with_proof` (used for `TooManyRejects` and OK responses) explicitly cross-checks both `content_hash` and `content_size` against the actual response body before trusting either value: [6](#0-5) 

The pool manager does validate `content_size` against actual response content for shares entering the pool: [7](#0-6) 

But `ResponsesTooLarge` embeds raw `CanisterHttpResponseShare` structs that need not have passed pool validation — a Byzantine proposer constructs them directly. The deferred batch signature verification only confirms the Byzantine node's signature is cryptographically valid over the inflated receipt; it does not constrain what `content_size` value was signed: [8](#0-7) 

## Impact Explanation

A Byzantine committee member signs a `CanisterHttpResponseReceipt` with `content_size = MAX_CANISTER_HTTP_PAYLOAD_SIZE + 1` while the actual response is small. A Byzantine block proposer embeds this share in a `ResponsesTooLarge` payload. The validator accepts the block; the canister receives a `ResponsesTooLarge` error and its cycles are burned without service. The attack can be repeated for any flexible HTTP outcall, causing targeted denial of service against specific canisters' HTTP requests and permanent cycles loss. This matches the allowed Medium impact: a constrained but meaningful attack requiring node-level Byzantine control with concrete user harm (application-level DoS against HTTP outcalls and cycles loss).

## Likelihood Explanation

The attack requires: (a) at least `min_known_ok_needed` Byzantine nodes in the `flex_committee` for the targeted request, and (b) a Byzantine block proposer. With `min_responses = 1` (or with unseen committee slots), `min_known_ok_needed = 1`, meaning a single Byzantine node that is both a committee member and the current block proposer suffices. The `flex_committee` is a small per-request subset of the full subnet committee, making it plausible that a single Byzantine node is included. Both conditions are well below the standard fault threshold `f = ⌊(n−1)/3⌋`.

## Recommendation

In the `ResponsesTooLarge` branch, do not trust `share.content.content_size()` for the size calculation. Require that each non-reject share in `all_seen_shares` is accompanied by its full response body (as `FlexibleCanisterHttpResponseWithProof` does for `TooManyRejects`), and compute the size from the actual response content after verifying both the content hash and content size match — mirroring the check already present in `validate_flexible_response_with_proof`: [6](#0-5) 

Alternatively, as a minimal mitigation, cap `content_size` at `MAX_CANISTER_HTTP_PAYLOAD_SIZE` during validation so that an inflated value cannot push `smallest_sum` above the limit unless the actual response would also exceed it.

## Proof of Concept

```
State-machine test sketch:
1. Create a Flexible request: committee = {node_0}, min_responses = 1.
2. Actual response is 100 bytes.
3. Byzantine node_0 signs CanisterHttpResponseReceipt {
       content_size: MAX_CANISTER_HTTP_PAYLOAD_SIZE as u32 + 1,
       content_hash: <any valid hash>,
       is_reject: false, ...
   }
4. Byzantine proposer builds:
   FlexibleCanisterHttpError::ResponsesTooLarge {
       callback_id,
       all_seen_shares: [share_0],   // share_0 has inflated content_size
       total_requests: 1,
       min_responses: 1,
   }
5. Call validate_canister_http_payload_impl with this payload.
6. Assert: result is Ok(()) — payload accepted despite actual response fitting.
7. Assert: canister receives ResponsesTooLarge error and cycles are burned.
```

`validate_response_share` passes (committee member, valid callback ID, registry version). Signature verification passes (node_0 genuinely signed the inflated receipt). `num_unseen = 0`, `min_known_ok_needed = 1`, `ok_entry_sizes = [inflated]`, `smallest_sum >> MAX` → `FlexibleResponsesNotTooLarge` guard is not triggered → block accepted.

### Citations

**File:** rs/types/types/src/batch/canister_http.rs (L48-53)
```rust
    ResponsesTooLarge {
        callback_id: CallbackId,
        all_seen_shares: Vec<CanisterHttpResponseShare>,
        total_requests: u32,
        min_responses: u32,
    },
```

**File:** rs/types/types/src/batch/canister_http.rs (L99-106)
```rust
    pub fn count_bytes_from_parts(
        canister_id: &CanisterId,
        content_size: usize,
        proof: &CanisterHttpResponseShare,
    ) -> usize {
        let response_size = CanisterHttpResponse::count_bytes_from_parts(canister_id, content_size);
        response_size + proof.count_bytes()
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

**File:** rs/https_outcalls/consensus/src/payload_builder.rs (L789-799)
```rust
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
```

**File:** rs/https_outcalls/consensus/src/payload_builder.rs (L810-818)
```rust
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
