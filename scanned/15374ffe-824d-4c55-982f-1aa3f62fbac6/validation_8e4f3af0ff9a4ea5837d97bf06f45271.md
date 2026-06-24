### Title
Byzantine Flexible-Committee Member Poisons Entire Response Group via Non-Candid Success Body — (`rs/https_outcalls/consensus/src/payload_builder.rs`)

### Summary

`flexible_ok_responses_into_consensus_response` requires every `Success(data)` entry to be valid Candid-encoded `CanisterHttpResponsePayload`, but `validate_canister_http_payload_impl` never checks this. A single Byzantine committee member can submit a `Success(non_candid_bytes)` response that passes all consensus validation, gets included in a `FlexibleCanisterHttpResponses` group by an honest proposer, and then causes `into_messages` to silently drop the entire group — permanently suppressing the canister's response.

---

### Finding Description

**The decode-or-drop logic in `flexible_ok_responses_into_consensus_response`:** [1](#0-0) 

```rust
let payloads: Vec<_> = response_group
    .responses
    .into_iter()
    .filter_map(|entry| match entry.response.content {
        CanisterHttpResponseContent::Success(data) => {
            Some(Decode!(&data, CanisterHttpResponsePayload).ok())
        }
        ...
    })
    .collect::<Option<_>>()?;   // ← None from ANY entry short-circuits the whole group
```

`filter_map` returns `Some(None)` when `Decode!` fails. `collect::<Option<_>>()` then returns `None` for the entire group, and the `?` propagates it — the `ConsensusResponse` is never produced.

**The validator never checks Candid encoding.** The full flexible-response validation loop only checks:
- Callback ID consistency
- Response count within `[min_responses, max_responses]`
- Signer is in `flex_committee`, no duplicate signers
- Content hash matches (`crypto_hash(&response) == proof.content_hash`)
- Content size matches
- `is_reject` flag matches
- Signature is valid (batch-verified) [2](#0-1) [3](#0-2) 

None of these checks touch the semantic content of `Success(data)` bytes.

**The proposer (`find_flexible_result`) also does not check Candid encoding.** It selects OK responses from the pool sorted by size, checking only committee membership, deduplication, pool availability, and payload budget: [4](#0-3) 

A Byzantine node's non-Candid `Success` response passes every filter here.

**The existing test confirms the drop:** [5](#0-4) 

`flexible_ok_responses_into_messages_decode_failure_is_skipped` explicitly asserts `responses.len() == 0` when one entry has invalid Candid — the test treats this as expected behavior, but it is the exact impact the attacker exploits.

**The code comment acknowledges but dismisses the risk:** [6](#0-5) 

> "Returns `None` if Candid decoding/encoding fails, which leads to skipping the delivery of this response. This should never occur, but if it does, eventually a timeout will gracefully end the outstanding callback."

A Byzantine node makes it occur deterministically.

---

### Impact Explanation

- The `ConsensusResponse` for the affected callback is never delivered to execution.
- The canister's outstanding HTTP outcall callback is never resolved until `CANISTER_HTTP_TIMEOUT_INTERVAL` expires.
- At timeout, the canister receives a `SysTransient` reject — but only after burning all cycles allocated for the request.
- A Byzantine node can repeat this for every flexible outcall it is assigned to, selectively suppressing responses for targeted canisters.

---

### Likelihood Explanation

- Requires control of exactly **one** subnet node that is a member of the `flex_committee` for the targeted request — well below the consensus fault threshold.
- The Byzantine node only needs to deviate in the HTTP adapter layer (return arbitrary bytes as the response body) and sign the share over the correct content hash of that body. No key theft or majority corruption is needed.
- The attack is fully deterministic and repeatable.
- `min_responses = 1` is the worst case (one Byzantine node suffices), but even with higher `min_responses`, if the Byzantine node's response is included alongside honest responses, the entire group is still dropped.

---

### Recommendation

**Option A (preferred):** Add a Candid decode check inside `validate_canister_http_payload_impl` for each `Success(data)` entry in `flexible_responses`, rejecting the payload with `InvalidArtifact` if any entry fails to decode as `CanisterHttpResponsePayload`.

**Option B:** Change `flexible_ok_responses_into_consensus_response` to skip (filter out) entries that fail Candid decoding rather than returning `None` for the entire group — consistent with how `Reject` entries are already filtered via `filter_map` returning `None`.

Option A is safer because it catches the malformed payload at validation time (before finalization), rather than silently degrading at execution time.

---

### Proof of Concept

```rust
// Feeds a FlexibleCanisterHttpResponses with one valid and one non-Candid
// Success entry to into_messages and asserts the response count is 0.
let callback_id = CallbackId::from(42);
let valid_data = Encode!(&CanisterHttpResponsePayload {
    status: 200, headers: vec![], body: vec![],
}).unwrap();
let valid_entry   = flexible_response(42, 0, &valid_data);
let invalid_entry = flexible_response(42, 1, b"not candid at all");

let payload = flexible_payload(vec![FlexibleCanisterHttpResponses {
    callback_id,
    responses: vec![valid_entry, invalid_entry],
}]);
let bytes = payload_to_bytes_max_4mb(payload);

let (responses, stats) = CanisterHttpPayloadBuilderImpl::into_messages(&bytes);
assert_eq!(responses.len(), 0);                          // response silently dropped
assert_eq!(stats.flexible_ok_responses_candid_failures, 1);
```

This is already present as `flexible_ok_responses_into_messages_decode_failure_is_skipped` and passes, confirming the drop. The missing piece — that a Byzantine node can produce a payload that passes `validate_payload` with such an entry — is the gap that makes this a real vulnerability.

### Citations

**File:** rs/https_outcalls/consensus/src/payload_builder.rs (L596-663)
```rust
        // Validate flexible responses
        for group in &payload.flexible_responses {
            let callback_id = group.callback_id;

            if !delivered_ids.insert(callback_id) {
                return invalid_artifact(InvalidCanisterHttpPayloadReason::DuplicateResponse(
                    callback_id,
                ));
            }

            // Look up the request context and verify it's a Flexible replication
            let context = http_contexts.get(&callback_id).ok_or(
                CanisterHttpPayloadValidationError::InvalidArtifact(
                    InvalidCanisterHttpPayloadReason::UnknownCallbackId(callback_id),
                ),
            )?;
            let Replication::Flexible {
                committee: flex_committee,
                min_responses,
                max_responses,
            } = &context.replication
            else {
                return invalid_artifact(InvalidCanisterHttpPayloadReason::InvalidPayloadSection(
                    callback_id,
                ));
            };

            // Check response count is within [min_responses, max_responses]
            let (min_responses, max_responses) = (*min_responses, *max_responses);
            let count = group.responses.len();
            if count < min_responses as usize || count > max_responses as usize {
                return invalid_artifact(
                    InvalidCanisterHttpPayloadReason::FlexibleResponseCountOutOfRange {
                        callback_id,
                        count,
                        min_responses,
                        max_responses,
                    },
                );
            }

            let mut seen_signers = HashSet::new();

            for response_with_proof in &group.responses {
                validate_flexible_response_with_proof(
                    response_with_proof,
                    callback_id,
                    flex_committee,
                    &mut seen_signers,
                    consensus_registry_version,
                    context.refund_status.per_replica_allowance,
                )
                .map_err(CanisterHttpPayloadValidationError::InvalidArtifact)?;

                if response_with_proof.response.content.is_reject() {
                    return invalid_artifact(
                        InvalidCanisterHttpPayloadReason::FlexibleRejectNotAllowedInOkResponses {
                            callback_id,
                        },
                    );
                }
            }

            // Defer signature verification.
            sig_inputs.extend(response_share_sig_inputs(
                group.responses.iter().map(|r| &r.proof),
            ));
        }
```

**File:** rs/https_outcalls/consensus/src/payload_builder.rs (L992-996)
```rust
/// Converts a [`FlexibleCanisterHttpResponses`] into a [`ConsensusResponse`].
///
/// Returns `None` if Candid decoding/encoding fails, which leads to skipping
/// the delivery of this response. This should never occur, but if it does,
/// eventually a timeout will gracefully end the outstanding callback.
```

**File:** rs/https_outcalls/consensus/src/payload_builder.rs (L1000-1014)
```rust
    let payloads: Vec<_> = response_group
        .responses
        .into_iter()
        .filter_map(|entry| match entry.response.content {
            CanisterHttpResponseContent::Success(data) => {
                Some(Decode!(&data, CanisterHttpResponsePayload).ok())
            }
            CanisterHttpResponseContent::Reject(_) => {
                // Unreachable: payload building/validation ensure
                // that there are no rejects in the ok-responses.
                None
            }
        })
        // Decoding errors short-circuit the collection and None is returned.
        .collect::<Option<_>>()?;
```

**File:** rs/https_outcalls/consensus/src/payload_builder/utils.rs (L147-197)
```rust
pub(crate) fn validate_flexible_response_with_proof(
    response_with_proof: &FlexibleCanisterHttpResponseWithProof,
    callback_id: CallbackId,
    flex_committee: &BTreeSet<NodeId>,
    seen_signers: &mut HashSet<NodeId>,
    consensus_registry_version: RegistryVersion,
    per_replica_allowance: Cycles,
) -> Result<(), InvalidCanisterHttpPayloadReason> {
    if response_with_proof.response.id != callback_id {
        return Err(
            InvalidCanisterHttpPayloadReason::FlexibleCallbackIdMismatch {
                callback_id,
                mismatched_id: response_with_proof.response.id,
            },
        );
    }

    validate_response_share(
        &response_with_proof.proof,
        callback_id,
        flex_committee,
        seen_signers,
        consensus_registry_version,
        per_replica_allowance,
    )?;

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

    let calculated_is_reject = response_with_proof.response.content.is_reject();
    if calculated_is_reject != response_with_proof.proof.content.is_reject() {
        return Err(InvalidCanisterHttpPayloadReason::IsRejectMismatch {
            metadata_is_reject: response_with_proof.proof.content.is_reject(),
            calculated_is_reject,
        });
    }

    Ok(())
```

**File:** rs/https_outcalls/consensus/src/payload_builder/utils.rs (L443-476)
```rust
    'outer: for (metadata, shares) in entries_sorted_asc {
        for &share in shares {
            if ok_responses.len() >= max_responses as usize {
                break 'outer;
            }
            let signer = share.signature.signer;
            if !committee.contains(&signer) || !seen_signers.insert(signer) {
                continue;
            }
            let Some(response) = pool_access.get_response_content_by_hash(&metadata.content_hash)
            else {
                continue;
            };

            if matches!(response.content, Reject(_)) {
                reject_responses.push((response, share));
                continue;
            }

            let response_with_proof_size =
                FlexibleCanisterHttpResponseWithProof::count_bytes(&response, share);
            all_ok_shares_sorted_asc.push((share, response_with_proof_size));

            let new_total = NumBytes::new(
                (accumulated_size + ok_responses_size + response_with_proof_size) as u64,
            );
            if new_total >= max_payload_size {
                // We `continue` rather than `break` here, to further populate
                // the Vec later used to detect ResponsesTooLarge errors.
                continue;
            }
            ok_responses_size += response_with_proof_size;
            ok_responses.push((response, share));
        }
```

**File:** rs/https_outcalls/consensus/src/payload_builder/tests.rs (L3003-3026)
```rust
fn flexible_ok_responses_into_messages_decode_failure_is_skipped() {
    let callback_id = CallbackId::from(42);

    let valid_data = Encode!(&CanisterHttpResponsePayload {
        status: 200,
        headers: vec![],
        body: vec![],
    })
    .unwrap();
    let valid_entry = flexible_response(42, 0, &valid_data);
    let invalid_entry = flexible_response(42, 1, b"this is invalid candid");

    let payload = flexible_payload(vec![FlexibleCanisterHttpResponses {
        callback_id,
        responses: vec![valid_entry, invalid_entry],
    }]);
    let bytes = payload_to_bytes_max_4mb(payload);

    let (responses, stats) = CanisterHttpPayloadBuilderImpl::into_messages(&bytes);

    assert_eq!(responses.len(), 0);
    assert_eq!(stats.flexible_ok_responses, 0);
    assert_eq!(stats.flexible_ok_responses_candid_failures, 1);
}
```
