Audit Report

## Title
Missing Candid Decode Validation in `validate_canister_http_payload_impl` Allows Byzantine Committee Member to Stall Flexible HTTP Callbacks — (`rs/https_outcalls/consensus/src/payload_builder.rs`)

## Summary

`validate_canister_http_payload_impl` never Candid-decodes the raw bytes inside `CanisterHttpResponseContent::Success(data)` for flexible HTTP response groups, while `flexible_ok_responses_into_consensus_response` does and silently returns `None` on failure. A Byzantine subnet node that is both a committee member for a targeted flexible HTTP request and the block proposer can sign a `CanisterHttpResponse` whose `Success` bytes are arbitrary non-Candid data, include it in a proposed block that passes all validation checks, and cause `into_messages` to drop the entire group — delivering no `ConsensusResponse` for the callback until the 60-second `CANISTER_HTTP_TIMEOUT_INTERVAL` fires.

## Finding Description

**Validation gap — no Candid decode of `Success` bytes:**

`validate_canister_http_payload_impl` iterates `payload.flexible_responses` and for each group performs: duplicate-ID check, context lookup, response-count range check, per-entry `validate_flexible_response_with_proof` (callback-ID consistency, signer membership, content hash, content size, is-reject flag), reject-not-allowed check, and deferred batch signature verification. At no point does it inspect whether the raw bytes of `CanisterHttpResponseContent::Success(data)` are valid Candid. [1](#0-0) 

**`validate_flexible_response_with_proof` only checks structural integrity, not Candid validity:**

The per-entry check verifies `crypto_hash(&response)` matches the proof's `content_hash`, and `response.content.count_bytes()` matches `content_size`. Both checks operate on the raw byte slice and pass regardless of whether those bytes are valid Candid. [2](#0-1) 

**Execution side — Candid decode with silent `None` on failure:**

`flexible_ok_responses_into_consensus_response` calls `Decode!(&data, CanisterHttpResponsePayload).ok()` for every `Success` entry. If any decode returns `None`, `.collect::<Option<_>>()` short-circuits and the function returns `None`. [3](#0-2) 

**`into_messages` silently drops the `None`:**

The `None` result increments `flexible_ok_responses_candid_failures` and is then discarded by `.flatten()`. No `ConsensusResponse` is emitted for that `callback_id`. [4](#0-3) 

**Exploit construction:**

A Byzantine node that is a member of `flex_committee` for a targeted request can:
1. Construct a `CanisterHttpResponse` with `content = Success(b"arbitrary non-candid bytes")`.
2. Compute `crypto_hash` of that response and build a `CanisterHttpResponseMetadata` with that hash, correct `content_size`, and `is_reject = false`.
3. Sign the metadata with their own key (valid committee-member signature).
4. When scheduled as block proposer, include this `FlexibleCanisterHttpResponseWithProof` in the payload, satisfying the `[min_responses, max_responses]` count constraint if `min_responses = 1`, or colluding with other Byzantine committee members otherwise.

The block passes `validate_canister_http_payload_impl` on every honest replica, is finalized, and then `into_messages` drops the group deterministically on all replicas.

## Impact Explanation

Every honest replica executes `into_messages` deterministically on the finalized block. Because the drop is deterministic, no replica delivers a `ConsensusResponse` for the targeted `callback_id`. The canister's callback is suspended until `CANISTER_HTTP_TIMEOUT_INTERVAL` (60 seconds) fires a timeout response. A Byzantine proposer can repeat this on every block it is scheduled to propose, targeting any open flexible HTTP callback for which it holds a committee membership. This constitutes a sustained, targeted application-level DoS against flexible HTTP outcalls — matching the **High** impact class: "Application/platform-level DoS, crash, consensus blocking, certified-state disruption, or subnet availability impact not based on raw volumetric DDoS." [5](#0-4) 

## Likelihood Explanation

The attack requires the Byzantine node to satisfy two conditions simultaneously: (1) be a member of `flex_committee` for the targeted request, and (2) be the block proposer for a block that includes that request's response. Block proposership rotates deterministically across all subnet nodes, so condition (2) is met regularly. Condition (1) depends on how committees are assigned; if committees are drawn from the full subnet membership, a Byzantine node below the `f < n/3` threshold will be in committees regularly. No threshold collusion is required when `min_responses = 1`. The crafted payload requires only that the `Success` bytes be non-Candid (trivially constructable) and that the Byzantine node sign their own metadata — standard cryptographic operations requiring no leaked keys or external compromise. [6](#0-5) 

## Recommendation

Add a Candid-decode check for `CanisterHttpResponseContent::Success(data)` inside the `flexible_responses` loop in `validate_canister_http_payload_impl`, mirroring exactly what `flexible_ok_responses_into_consensus_response` does at execution time. For each `response_with_proof` whose content is `Success(data)`, attempt `Decode!(&data, CanisterHttpResponsePayload)` and, on failure, return `invalid_artifact(InvalidCanisterHttpPayloadReason::FlexibleResponseCandidDecodeError { callback_id })`. This closes the semantic gap between validation and execution and ensures that any payload passing validation will also produce a `ConsensusResponse` in `into_messages`. [7](#0-6) 

## Proof of Concept

The existing test `flexible_ok_responses_into_messages_decode_failure_is_skipped` at lines 3002–3026 is a direct local-runnable proof of concept. It constructs a `FlexibleCanisterHttpResponses` group containing one valid Candid entry and one `b"this is invalid candid"` entry, calls `into_messages`, and asserts `responses.len() == 0` and `flexible_ok_responses_candid_failures == 1` — confirming the silent drop path. [8](#0-7) 

To complete the end-to-end exploit demonstration, a companion test should call `validate_canister_http_payload_impl` (via `validate_payload`) on the same payload inside a `setup_test_with_flexible_context` harness and assert it returns `Ok(())` — confirming that the payload passes validation despite containing non-Candid bytes. The `flexible_response` test helper already constructs entries with arbitrary raw bytes as `Success` content and produces structurally valid proofs (correct hash, size, and committee-member signature), so the companion test requires no additional infrastructure. [9](#0-8)

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

**File:** rs/https_outcalls/consensus/src/payload_builder.rs (L961-969)
```rust
        let flexible_ok_responses = messages
            .flexible_responses
            .into_iter()
            .map(flexible_ok_responses_into_consensus_response)
            .inspect(|result| match result {
                Some(_) => stats.flexible_ok_responses += 1,
                None => stats.flexible_ok_responses_candid_failures += 1,
            })
            .flatten();
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

**File:** rs/types/types/src/canister_http.rs (L78-79)
```rust
/// Time after which a response is considered timed out and a timeout error will be returned to execution
pub const CANISTER_HTTP_TIMEOUT_INTERVAL: Duration = Duration::from_secs(60);
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

**File:** rs/https_outcalls/consensus/src/payload_builder/tests.rs (L4816-4829)
```rust
fn flexible_response(
    callback_id: u64,
    signer_node: u64,
    content: &[u8],
) -> FlexibleCanisterHttpResponseWithProof {
    let (response, metadata) = test_response_and_metadata_with_content(
        callback_id,
        CanisterHttpResponseContent::Success(content.to_vec()),
    );
    FlexibleCanisterHttpResponseWithProof {
        response,
        proof: metadata_to_share(signer_node, &metadata),
    }
}
```
