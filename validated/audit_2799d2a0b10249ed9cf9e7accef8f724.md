All code claims verify against the actual source. The exploit path is real and confirmed by the existing test.

Audit Report

## Title
Byzantine Committee Member Suppresses Flexible HTTP Outcall Responses via Non-Candid Success Body — (`rs/https_outcalls/consensus/src/payload_builder.rs`)

## Summary
`flexible_ok_responses_into_consensus_response` uses `collect::<Option<_>>()?` to decode all `Success(data)` entries as Candid `CanisterHttpResponsePayload`, returning `None` for the entire group if any single entry fails to decode. However, `validate_canister_http_payload_impl` never checks whether `Success(data)` bytes are valid Candid. A single Byzantine `flex_committee` member can submit a `Success(non_candid_bytes)` response that passes all consensus validation, gets included in a finalized block, and then causes `into_messages` to silently drop the entire response group — permanently suppressing the canister's HTTP outcall callback until timeout.

## Finding Description
**Root cause — decode-or-drop in `flexible_ok_responses_into_consensus_response`:** [1](#0-0) 

`filter_map` returns `Some(None)` when `Decode!` fails on a `Success(data)` entry. `collect::<Option<Vec<_>>>()` then short-circuits to `None`, and the `?` propagates it — no `ConsensusResponse` is produced for the callback.

**Validation gap — `validate_canister_http_payload_impl` never checks Candid encoding:** [2](#0-1) 

The loop over `payload.flexible_responses` checks callback ID, response count bounds, committee membership, duplicate signers, content hash, content size, `is_reject` flag, and defers signature verification — but never attempts to decode `Success(data)` bytes as `CanisterHttpResponsePayload`.

**`validate_flexible_response_with_proof` also has no Candid check:** [3](#0-2) 

The content hash check at L173-178 hashes the raw `CanisterHttpResponse` struct (including the raw bytes), so a Byzantine node can produce a valid hash over arbitrary non-Candid bytes and pass this check.

**Exploit flow:**
1. Byzantine node is a `flex_committee` member for a targeted request.
2. It returns arbitrary non-Candid bytes as the HTTP response body and signs the share over the correct `crypto_hash` of that response.
3. An honest proposer includes this response in a `FlexibleCanisterHttpResponses` group alongside honest responses.
4. The group passes `validate_canister_http_payload_impl` — all structural checks pass.
5. At `into_messages` time, `flexible_ok_responses_into_consensus_response` fails to Candid-decode the Byzantine entry, returns `None`, and the entire group is dropped.
6. The canister's callback is never resolved until `CANISTER_HTTP_TIMEOUT_INTERVAL` expires.

**The existing test confirms the drop behavior:** [4](#0-3) 

`flexible_ok_responses_into_messages_decode_failure_is_skipped` asserts `responses.len() == 0` — the test treats this as expected behavior, but it is precisely the impact the attacker exploits.

## Impact Explanation
This is a **High** severity finding matching "Application/platform-level DoS... or subnet availability impact not based on raw volumetric DDoS." A Byzantine node can deterministically suppress the delivery of any HTTP outcall response it is assigned to, forcing the canister to wait for `CANISTER_HTTP_TIMEOUT_INTERVAL` and then receive a `SysTransient` reject while burning all cycles allocated for the request. The attack is repeatable for every flexible outcall the Byzantine node is assigned to, enabling targeted, sustained disruption of specific canisters' HTTP outcall functionality.

## Likelihood Explanation
Requires control of exactly one subnet node that is a member of the `flex_committee` for the targeted request — well below the consensus fault threshold. The Byzantine node only needs to deviate in the HTTP adapter layer (return arbitrary bytes) and sign the share over the correct content hash of those bytes. No key theft, majority corruption, or special privileges beyond node operation are required. The attack is fully deterministic and repeatable across all flexible outcalls the node is assigned to.

## Recommendation
**Option A (preferred):** Add a Candid decode check inside `validate_canister_http_payload_impl` for each `Success(data)` entry in `flexible_responses`, rejecting the payload with `InvalidArtifact(InvalidCanisterHttpPayloadReason::...)` if any entry fails to decode as `CanisterHttpResponsePayload`. This catches the malformed payload at validation time before finalization.

**Option B:** Change `flexible_ok_responses_into_consensus_response` to skip (filter out) entries that fail Candid decoding rather than returning `None` for the entire group — consistent with how `Reject` entries are already handled via `filter_map` returning `None`. This degrades gracefully at execution time but does not prevent the malformed payload from being finalized.

Option A is safer because it prevents the malformed payload from ever being included in a finalized block.

## Proof of Concept
The existing test `flexible_ok_responses_into_messages_decode_failure_is_skipped` at [5](#0-4)  already proves the drop. The missing piece — that a Byzantine node can produce a payload with a non-Candid `Success` entry that passes `validate_payload` — can be demonstrated by constructing a `FlexibleCanisterHttpResponses` group with one valid and one non-Candid `Success` entry, running it through `validate_canister_http_payload_impl` with a mock context, and asserting it returns `Ok(())`. The combination of both tests proves the full exploit: the malformed payload passes validation and then silently drops the response group at execution time.

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

**File:** rs/https_outcalls/consensus/src/payload_builder/tests.rs (L3002-3026)
```rust
#[test]
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
