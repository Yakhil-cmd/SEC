All four steps of the claim check out against the actual code. Let me verify the `retrieve_context` behavior as well.

All four steps of the claim are verified against the actual code. The vulnerability is real.

Audit Report

## Title
Byzantine Subnet Node Can Permanently Freeze a Flexible HTTP Callback via Non-Candid Response Body — (`rs/https_outcalls/consensus/src/payload_builder.rs`)

## Summary
`validate_flexible_response_with_proof` never Candid-decodes response bodies, so a Byzantine committee member can submit a cryptographically valid share whose body is arbitrary bytes. When the block is finalized, `flexible_ok_responses_into_consensus_response` silently returns `None` on Candid failure, dropping the `ConsensusResponse`. Because `parse_past_payload_ids` still records the `callback_id` as delivered, the timeout path is permanently suppressed and the canister callback is frozen forever with no response and no refund.

## Finding Description

**Step 1 — Validation never checks Candid encoding.**

`validate_flexible_response_with_proof` (`rs/https_outcalls/consensus/src/payload_builder/utils.rs`, lines 147–198) verifies callback-ID consistency, committee membership, registry version, `crypto_hash` match, content size, and `is_reject` flag. [1](#0-0) 

It never attempts `Decode!(&data, CanisterHttpResponsePayload)`. A Byzantine committee member can place arbitrary bytes in `CanisterHttpResponseContent::Success(data)`, compute the correct `crypto_hash` over that response, sign the metadata, and the proof passes every check.

**Step 2 — `into_messages` silently drops the callback on Candid failure.**

`flexible_ok_responses_into_consensus_response` Candid-decodes each body via `.collect::<Option<_>>()?`, which short-circuits to `None` on any decode error. [2](#0-1) 

The caller chains `.flatten()` over the `Option`, so the `ConsensusResponse` for that `callback_id` is silently omitted from the returned vec. [3](#0-2) 

The comment at lines 992–996 claims "eventually a timeout will gracefully end the outstanding callback" — this is incorrect (see Step 3). [4](#0-3) 

**Step 3 — The timeout fallback is permanently suppressed.**

`parse_past_payload_ids` extracts the `callback_id` from every finalized `FlexibleResponses` message and adds it to `delivered_ids`. [5](#0-4) [6](#0-5) 

In `get_canister_http_payload_impl`, the very first check per open context is:

```rust
if delivered_ids.contains(callback_id) {
    continue;
}
``` [7](#0-6) 

The timeout check at line 234 is never reached for this `callback_id`. No timeout is ever proposed or finalized.

**Step 4 — Execution-layer context is permanently leaked.**

`retrieve_context` removes a `CanisterHttpRequestContext` only when a `ConsensusResponse` is processed. [8](#0-7) 

Since no `ConsensusResponse` was delivered (Step 2), the context remains in `canister_http_request_contexts` indefinitely. The consensus layer skips it (Step 3). The callback slot is permanently leaked.

**Existing test confirms the drop:**

`flexible_ok_responses_into_messages_decode_failure_is_skipped` explicitly asserts `responses.len() == 0` and `flexible_ok_responses_candid_failures == 1` when one entry carries invalid Candid bytes. [9](#0-8) 

## Impact Explanation

The targeted canister's open callback is permanently frozen: no response is delivered, no timeout is generated, the `CanisterHttpRequestContext` leaks forever, cycles attached to the request are never refunded, and the canister cannot make further HTTP outcalls once it exhausts its callback slots. This is a targeted, repeatable application/platform-level DoS matching the **High** impact class: *Application/platform-level DoS, crash, consensus blocking, certified-state disruption, or subnet availability impact not based on raw volumetric DDoS* ($2,000–$10,000).

## Likelihood Explanation

The attack requires only a single Byzantine subnet node operating below the `f < n/3` fault-tolerance threshold — an explicitly modeled adversary in the IC protocol. The node must be a committee member for the targeted outcall (all subnet nodes are eligible) and submit a share with non-Candid bytes. With `min_responses = 1`, no coordination with other nodes is needed. An honest block proposer will include the share because block-building also performs no Candid check. The attack is local-testable and repeatable against any canister using flexible HTTP outcalls.

## Recommendation

1. **In `validate_canister_http_payload_impl`**, add a Candid-decode check for each `CanisterHttpResponseContent::Success(data)` in `flexible_responses`, rejecting the payload with `InvalidCanisterHttpPayloadReason` if any body fails `Decode!(&data, CanisterHttpResponsePayload)`.
2. **Alternatively**, change `flexible_ok_responses_into_consensus_response` to emit a `SysTransient` reject `ConsensusResponse` on decode failure instead of returning `None`, ensuring the callback is always resolved.
3. **Remove or correct the misleading comment** at lines 992–996 — the timeout fallback does not work because `delivered_ids` suppresses it.

## Proof of Concept

The existing test `flexible_ok_responses_into_messages_decode_failure_is_skipped` already proves Steps 2–5 in isolation. To demonstrate the full end-to-end freeze:

```rust
// 1. Byzantine committee member creates a share with non-Candid body
let non_candid_body = b"this is not candid".to_vec();
let response = CanisterHttpResponse {
    id: callback_id,
    content: CanisterHttpResponseContent::Success(non_candid_body),
    ..
};
// Compute correct hash and sign (all validation checks pass)
let hash = crypto_hash(&response);
// ... create share with hash, sign it, submit to pool ...

// 2. Honest block proposer includes it (no Candid check in building/validation)
// validate_payload returns Ok(())

// 3. Block is finalized; into_messages drops the ConsensusResponse silently
let (responses, stats) = CanisterHttpPayloadBuilderImpl::into_messages(&bytes);
assert_eq!(responses.len(), 0);
assert_eq!(stats.flexible_ok_responses_candid_failures, 1);

// 4. In all subsequent blocks, callback_id is in delivered_ids → no timeout ever proposed
// CanisterHttpRequestContext remains in canister_http_request_contexts forever
```

### Citations

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

**File:** rs/https_outcalls/consensus/src/payload_builder.rs (L231-233)
```rust
                if delivered_ids.contains(callback_id) {
                    continue;
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

**File:** rs/https_outcalls/consensus/src/payload_builder/parse.rs (L96-118)
```rust
pub(crate) fn parse_past_payload_ids(
    past_payloads: &[PastPayload],
    log: &ReplicaLogger,
) -> HashSet<CallbackId> {
    past_payloads
        .iter()
        .flat_map(|payload| {
            slice_to_messages::<CanisterHttpResponseMessage>(payload.payload).unwrap_or_else(
                |err| {
                    error!(
                        log,
                        "Failed to parse CanisterHttp past payload for height {}. Error: {}",
                        payload.height,
                        err
                    );
                    vec![]
                },
            )
        })
        .filter_map(get_id_from_message)
        .map(CallbackId::new)
        .collect()
}
```

**File:** rs/https_outcalls/consensus/src/payload_builder/parse.rs (L130-130)
```rust
        Some(MessageType::FlexibleResponses(flex_responses)) => Some(flex_responses.callback_id),
```

**File:** rs/replicated_state/src/metadata_state/subnet_call_context_manager.rs (L311-323)
```rust
            .or_else(|| {
                self.canister_http_request_contexts
                    .remove(&callback_id)
                    .map(|context| {
                        info!(
                            logger,
                            "Received the response for HttpRequest with callback id {:?} from {:?}",
                            context.request.sender_reply_callback,
                            context.request.sender
                        );
                        SubnetCallContext::CanisterHttpRequest(context)
                    })
            })
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
