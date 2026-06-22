### Title
Byzantine `content_size` Inflation in `ResponsesTooLarge` Enables False Callback Termination — (`rs/https_outcalls/consensus/src/payload_builder.rs`)

---

### Summary

The `ResponsesTooLarge` branch of `validate_canister_http_payload_impl` computes the "too large" proof entirely from the `content_size` field embedded in each `CanisterHttpResponseShare`'s signed metadata. Because `content_size` is self-reported by the signing node and is never cross-checked against an actual response body in this path, a Byzantine committee member can sign a share with an arbitrarily inflated `content_size`. A Byzantine block proposer can then craft a `ResponsesTooLarge` payload using only those inflated shares, causing the validator to accept a false "responses too large" proof and permanently deliver an error to the canister — even though honest nodes could have delivered a response that fits within `MAX_CANISTER_HTTP_PAYLOAD_SIZE`.

---

### Finding Description

**Root cause — `content_size` is trusted without a response-body witness in the `ResponsesTooLarge` path.**

`validate_response_share` (the only per-share check called for `ResponsesTooLarge`) validates:
- callback-id consistency
- no duplicate signers
- committee membership
- registry version [1](#0-0) 

It does **not** validate `content_size` against any actual response body. After `validate_response_share` passes, the validator computes `ok_entry_sizes` directly from `share.content.content_size()`: [2](#0-1) 

The `smallest_sum` of the `min_known_ok_needed` smallest entries is then compared to `MAX_CANISTER_HTTP_PAYLOAD_SIZE`: [3](#0-2) 

Signatures are batch-verified at the end, so the Byzantine nodes must actually sign the inflated metadata — but Byzantine nodes are free to sign any `content_size` value they choose.

**Contrast with `TooManyRejects`**, which uses `FlexibleCanisterHttpResponseWithProof` (response body included) and explicitly cross-checks `content_size` against the actual body: [4](#0-3) 

The `ResponsesTooLarge` variant carries only `Vec<CanisterHttpResponseShare>` — no response bodies at all: [5](#0-4) 

**Concrete attack (C=5, M=3, fault\_threshold=2, f=1):**

1. One Byzantine committee member signs an OK `CanisterHttpResponseShare` with `content_size = MAX_CANISTER_HTTP_PAYLOAD_SIZE + 1` (any value > MAX). The signature covers the full `CanisterHttpResponseReceipt` including `content_size`, so the batch-verify at line 824 passes.
2. Two honest nodes happen to return HTTP-level reject responses (plausible when the upstream server returns an error for some replicas). Their reject shares are gossiped and available to the Byzantine block proposer.
3. The Byzantine block proposer builds `ResponsesTooLarge { all_seen_shares: [byz_ok_share, honest_reject_1, honest_reject_2], total_requests: 5, min_responses: 3 }`.
4. Validator computes:
   - `num_unseen = 5 − 3 = 2`
   - `min_known_ok_needed = 3 − 2 = 1`
   - `ok_entry_sizes = [inflated_size]` (1 element ≥ 1 needed ✓)
   - `smallest_sum = inflated_size > MAX` → payload accepted ✓
5. The two unseen honest nodes could have provided small OK responses, but the validator never checks this.
6. `into_messages` delivers `FlexibleHttpGlobalError::ResponsesTooLarge` to the canister; the callback is permanently closed and cycles are burned. [6](#0-5) 

---

### Impact Explanation

- **Permanent callback denial**: once the `ResponsesTooLarge` error is finalized in a block, the callback is closed and cannot be retried.
- **Cycles burn**: the canister's cycles for the HTTP outcall are consumed.
- **Targeted**: the attacker can selectively target specific `callback_id`s, enabling denial-of-service against specific canisters or workflows.
- **Scalable**: a single Byzantine committee member, whenever it is selected as block proposer, can terminate any in-flight flexible HTTP outcall for which it is a committee member.

---

### Likelihood Explanation

- Requires **one Byzantine committee member** (f=1 suffices, well below the fault threshold).
- Requires that Byzantine node to be **selected as block proposer** — this occurs with probability f/subnet\_size per round, so it is a matter of time.
- Requires **enough honest reject shares** to reduce `num_unseen` so that `min_known_ok_needed ≥ 1`. This is satisfied whenever the upstream HTTP server returns errors for some replicas, which is common in practice (transient network errors, rate limiting, etc.). Alternatively, the attacker can include Byzantine reject shares from other Byzantine nodes to reduce `num_unseen`.
- No privileged access, no key leakage, no governance majority required.

---

### Recommendation

The `ResponsesTooLarge` path must require that each OK share in `all_seen_shares` is accompanied by the actual response body (i.e., use `FlexibleCanisterHttpResponseWithProof` instead of bare `CanisterHttpResponseShare`), and validate `content_size` against the actual body exactly as `validate_flexible_response_with_proof` does: [7](#0-6) 

Alternatively, if carrying full response bodies in `ResponsesTooLarge` is undesirable for size reasons, the `content_size` field must be treated as an **upper bound** (i.e., the proof is only accepted if even the minimum possible sizes — zero for unseen nodes — still exceed `MAX`), or the threshold logic must be restructured so that Byzantine inflation of `content_size` cannot cause a false positive.

---

### Proof of Concept

```rust
// State-machine test sketch
// Committee: nodes 0..5, min_responses=3, fault_threshold=2, f=1 (node 0 is Byzantine)

// Step 1: Byzantine node 0 signs an OK share with inflated content_size
let inflated_size = MAX_CANISTER_HTTP_PAYLOAD_SIZE as u32 + 1;
let byz_ok_share = metadata_share_with_content_size(callback_id.get(), 0, inflated_size);
// (Byzantine node actually signs this — batch-verify will pass)

// Step 2: Two honest nodes (1, 2) legitimately send reject shares
let honest_reject_1 = reject_metadata_share(callback_id.get(), 1);
let honest_reject_2 = reject_metadata_share(callback_id.get(), 2);

// Step 3: Byzantine block proposer crafts ResponsesTooLarge
let payload = CanisterHttpPayload {
    flexible_errors: vec![FlexibleCanisterHttpError::ResponsesTooLarge {
        callback_id,
        all_seen_shares: vec![byz_ok_share, honest_reject_1, honest_reject_2],
        total_requests: 5,   // matches committee size
        min_responses: 3,    // matches context
    }],
    ..Default::default()
};

// Step 4: Validator accepts (num_unseen=2, min_known_ok_needed=1, smallest_sum > MAX)
let result = pb.validate_payload(..., &payload_to_bytes(payload), &[]);
assert_matches!(result, Ok(()));  // BUG: should be Err

// Step 5: Honest nodes 3 and 4 (unseen) had small OK responses that would have fit
// — the callback is now permanently closed with a false error
```

### Citations

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

**File:** rs/types/types/src/batch/canister_http.rs (L48-53)
```rust
    ResponsesTooLarge {
        callback_id: CallbackId,
        all_seen_shares: Vec<CanisterHttpResponseShare>,
        total_requests: u32,
        min_responses: u32,
    },
```
