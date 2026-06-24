Let me look at the execution-layer handling of duplicate responses and the `push_response_bitcoin` call chain more carefully.

Now I have a complete picture of the full exploit chain. Let me write up the final assessment.

---

### Title
Missing `delivered_ids` Check in `BitcoinPayloadBuilder::validate_payload` Allows Byzantine Block Proposer to Inject Duplicate Bitcoin Adapter Responses — (`rs/bitcoin/consensus/src/payload_builder.rs`)

### Summary

`BitcoinPayloadBuilder::validate_payload` computes `delivered_ids` from `past_payloads` but never uses the set to reject responses whose `callback_id` was already delivered. A Byzantine block proposer can craft a payload containing a response for a `callback_id` present in `past_payloads`; the validator returns `Ok(())`, the block is finalized, and execution pushes a duplicate `ConsensusResponse` into the `consensus_queue`, causing the Bitcoin canister to receive a spurious second response for an already-resolved callback.

### Finding Description

In `validate_payload` (lines 357–397 of `rs/bitcoin/consensus/src/payload_builder.rs`):

```rust
let delivered_ids = parse::parse_past_payload_ids(past_payloads, &self.log);
// delivered_ids is computed but never consulted again
let _ = self.validate_self_validating_payload_impl(...)?;
Ok(())
```

`delivered_ids` is assigned and immediately abandoned. [1](#0-0) 

Compare with `ChainKeyPayloadBuilder::validate_payload`, which passes `delivered_ids` into `validate_chain_key_payload_impl`, and `CanisterHttpPayloadBuilderImpl::validate_payload`, which passes it into `validate_canister_http_payload_impl`. [2](#0-1) 

`validate_self_validating_payload_impl` only checks for an empty payload and payload size — it has no knowledge of `past_payloads` at all. [3](#0-2) 

### Impact Explanation

**Execution layer does not fully compensate.**

`push_response` in `rs/replicated_state/src/bitcoin.rs` uses `get_mut` (not `remove`) for `GetSuccessorsResponse`, so the context remains in `bitcoin_get_successors_contexts` after the first delivery. A second call with the same `callback_id` therefore also succeeds and pushes a second `ConsensusResponse` into the `consensus_queue`. [4](#0-3) 

For `SendTransactionResponse`, `GetSuccessorsReject`, and `SendTransactionReject`, `push_response` performs **no existence check at all** — it unconditionally pushes to the `consensus_queue` regardless of whether the callback was already delivered. [5](#0-4) 

In `demux.rs`, errors from `push_response_bitcoin` are only logged at `debug` level and execution continues, so the duplicate entry reaches the scheduler. [6](#0-5) 

The scheduler then attempts to deliver the second `ConsensusResponse` to the Bitcoin canister's already-resolved callback. The callback has been removed from the callback table after the first delivery; the second delivery results in a "callback not found" condition, which can corrupt the canister's observable state or trigger a critical-error path in the scheduler.

### Likelihood Explanation

Requires a single Byzantine block proposer — one subnet node acting maliciously. This is within the IC's stated fault-tolerance assumption (f < n/3 Byzantine nodes). No privileged key, governance majority, or external compromise is needed. The proposer simply crafts a `SelfValidatingPayload` whose bytes encode a `BitcoinAdapterResponse` with a `callback_id` copied from any entry in `past_payloads`. The consensus validation layer will accept it.

### Recommendation

In `validate_payload`, after decoding the payload, iterate over the decoded responses and reject any whose `callback_id` is in `delivered_ids`:

```rust
let delivered_ids = parse::parse_past_payload_ids(past_payloads, &self.log);
// ... decode payload ...
for response in &payload {
    if delivered_ids.contains(&response.callback_id) {
        return Err(ValidationError::InvalidArtifact(
            consensus::InvalidPayloadReason::InvalidSelfValidatingPayload(
                InvalidSelfValidatingPayloadReason::DuplicateResponse,
            ),
        ));
    }
}
```

This mirrors the pattern already used by `ChainKeyPayloadBuilder` and `CanisterHttpPayloadBuilderImpl`.

### Proof of Concept

1. Encode a `BitcoinAdapterResponse` with `callback_id = 5` into `past_payload_bytes`.
2. Construct a `PastPayload` from those bytes.
3. Encode a second `BitcoinAdapterResponse` with `callback_id = 5` into `new_payload_bytes`.
4. Call `bitcoin_payload_builder.validate_payload(height, &proposal_context, &new_payload_bytes, &[past_payload])`.
5. **Observed**: returns `Ok(())`.
6. **Expected**: returns `Err(InvalidArtifact(InvalidSelfValidatingPayload(...)))`.

The test structure already exists in `rs/bitcoin/consensus/src/payload_builder/tests.rs` (the `skips_past_callback_ids` test at line 323 validates `build_payload` skips past IDs, but no analogous test exists for `validate_payload`). [7](#0-6)

### Citations

**File:** rs/bitcoin/consensus/src/payload_builder.rs (L234-251)
```rust
    fn validate_self_validating_payload_impl(
        &self,
        payload: &SelfValidatingPayload,
        validation_context: &ValidationContext,
    ) -> Result<NumBytes, SelfValidatingPayloadValidationError> {
        let since = Instant::now();

        // An empty block is always valid.
        if *payload == SelfValidatingPayload::default() {
            return Ok(0.into());
        }

        self.metrics
            .observe_validate_duration(VALIDATION_STATUS_VALID, since);
        let size = NumBytes::new(payload.count_bytes() as u64);

        Ok(size)
    }
```

**File:** rs/bitcoin/consensus/src/payload_builder.rs (L369-382)
```rust
        let delivered_ids = parse::parse_past_payload_ids(past_payloads, &self.log);
        let payload = parse::bytes_to_payload(payload).map_err(|e| {
            ValidationError::InvalidArtifact(
                consensus::InvalidPayloadReason::InvalidSelfValidatingPayload(
                    InvalidSelfValidatingPayloadReason::DecodeError(e),
                ),
            )
        })?;
        let num_responses = payload.len();

        let _ = self.validate_self_validating_payload_impl(
            &SelfValidatingPayload::new(payload),
            proposal_context.validation_context,
        )?;
```

**File:** rs/consensus/chain_key/src/lib.rs (L566-577)
```rust
        let delivered_ids = parse_past_payload_ids(past_payloads, &self.log);
        let payload = bytes_to_chain_key_payload(payload).map_err(|e| {
            invalid_artifact(InvalidChainKeyPayloadReason::DeserializationFailed(e))
        })?;

        self.validate_chain_key_payload_impl(
            payload,
            request_expiry,
            valid_keys,
            state.get_ref(),
            delivered_ids,
        )
```

**File:** rs/replicated_state/src/bitcoin.rs (L31-62)
```rust
            let callback_id = CallbackId::from(response.callback_id);
            let context = state
                .metadata
                .subnet_call_context_manager
                .bitcoin_get_successors_contexts
                .get_mut(&callback_id)
                .ok_or_else(|| StateError::BitcoinNonMatchingResponse {
                    callback_id: callback_id.get(),
                })?;

            let payload = match maybe_split_response(r) {
                Ok((initial_response, follow_ups)) => {
                    // Store the follow-ups for later (overwrites previous ones).
                    state
                        .metadata
                        .bitcoin_get_successors_follow_up_responses
                        .insert(context.request.sender(), follow_ups);

                    Payload::Data(initial_response.encode())
                }
                Err(err) => Payload::Reject(RejectContext::new(
                    RejectCode::CanisterError,
                    format!("Received invalid response from adapter: {err:?}"),
                )),
            };

            // Add response to the consensus queue.
            state
                .consensus_queue
                .push(ConsensusResponse::new(callback_id, payload));

            Ok(())
```

**File:** rs/replicated_state/src/bitcoin.rs (L64-103)
```rust
        BitcoinAdapterResponseWrapper::SendTransactionResponse(_) => {
            // Retrieve the associated request from the call context manager.
            let callback_id = CallbackId::from(response.callback_id);
            // The response to a `send_transaction` call is always the empty blob.
            let payload = Payload::Data(EmptyBlob.encode());

            // Add response to the consensus queue.
            state
                .consensus_queue
                .push(ConsensusResponse::new(callback_id, payload));

            Ok(())
        }
        BitcoinAdapterResponseWrapper::GetSuccessorsReject(reject) => {
            // Retrieve the associated request from the call context manager.
            let callback_id = CallbackId::from(response.callback_id);
            let reject_payload =
                Payload::Reject(RejectContext::new(reject.reject_code, reject.message));

            // Add response to the consensus queue.
            state
                .consensus_queue
                .push(ConsensusResponse::new(callback_id, reject_payload));

            Ok(())
        }
        BitcoinAdapterResponseWrapper::SendTransactionReject(reject) => {
            // Retrieve the associated request from the call context manager.
            let callback_id = CallbackId::from(response.callback_id);
            let reject_payload =
                Payload::Reject(RejectContext::new(reject.reject_code, reject.message));

            // Add response to the consensus queue.
            state
                .consensus_queue
                .push(ConsensusResponse::new(callback_id, reject_payload));

            Ok(())
        }
    }
```

**File:** rs/messaging/src/routing/demux.rs (L86-94)
```rust
        for response in batch_messages.bitcoin_adapter_responses.into_iter() {
            state.push_response_bitcoin(response).unwrap_or_else(|err| {
                debug!(
                    self.log,
                    "Error pushing the response from bitcoin adapter {}",
                    err.to_string()
                )
            });
        }
```

**File:** rs/bitcoin/consensus/src/payload_builder/tests.rs (L323-391)
```rust
    let registry_client = mock_registry_client(MAX_BLOCK_PAYLOAD_SIZE);

    bitcoin_payload_builder_test(
        bitcoin_mainnet_adapter_client,
        bitcoin_testnet_adapter_client,
        state_manager,
        registry_client,
        |proposal_context, bitcoin_payload_builder| {
            let past_payload = FakeSelfValidatingPayloadBuilder::new()
                .with_responses(vec![BitcoinAdapterResponse {
                    response: BitcoinAdapterResponseWrapper::GetSuccessorsResponse(
                        GetSuccessorsResponseComplete {
                            blocks: vec![],
                            next: vec![],
                        },
                    ),
                    callback_id: 0,
                }])
                .build();
            let past_payload = parse::payload_to_bytes(
                &past_payload,
                SELF_VALIDATING_PAYLOAD_BYTE_LIMIT,
                &no_op_logger(),
            );
            let past_payloads = vec![PastPayload {
                height: Height::from(0),
                time: UNIX_EPOCH,
                block_hash: CryptoHashOf::from(CryptoHash(vec![])),
                payload: &past_payload,
            }];

            let expected_payload = FakeSelfValidatingPayloadBuilder::new()
                .with_responses(vec![BitcoinAdapterResponse {
                    response: BitcoinAdapterResponseWrapper::GetSuccessorsResponse(
                        GetSuccessorsResponseComplete {
                            blocks: vec![],
                            next: vec![],
                        },
                    ),
                    callback_id: 1,
                }])
                .build();
            let expected_payload = parse::payload_to_bytes(
                &expected_payload,
                SELF_VALIDATING_PAYLOAD_BYTE_LIMIT,
                &no_op_logger(),
            );

            let payload = bitcoin_payload_builder.build_payload(
                Height::new(1),
                SELF_VALIDATING_PAYLOAD_BYTE_LIMIT,
                &past_payloads,
                proposal_context.validation_context,
            );
            let validation_result = bitcoin_payload_builder.validate_payload(
                Height::new(1),
                &proposal_context,
                &payload,
                &past_payloads,
            );
            assert!(
                validation_result.is_ok(),
                "validation did not pass {validation_result:?}"
            );

            assert_eq!(payload, expected_payload);
        },
    );
}
```
