### Title
Missing Intra-Payload Duplicate `callback_id` Check in Bitcoin Payload Validation Allows Byzantine Proposer to Inject Multiple Responses — (`rs/bitcoin/consensus/src/payload_builder.rs`)

---

### Summary

`validate_payload` (the `BatchPayloadBuilder` impl) never checks for duplicate `callback_id` values within the proposed `SelfValidatingPayload`. Because `push_response` is not idempotent, a Byzantine block proposer can craft a payload containing N copies of the same `BitcoinAdapterResponse`, pass validation on all honest nodes, and cause N entries to be pushed into `consensus_queue` for a single pending request.

---

### Finding Description

**Gap 1 — `validate_payload` performs no intra-payload deduplication.** [1](#0-0) 

After decoding the payload bytes, `validate_payload` calls `validate_self_validating_payload_impl`, which returns `Ok` for any non-empty payload: [2](#0-1) 

There is no loop, set, or assertion that checks whether two entries in the decoded `Vec<BitcoinAdapterResponse>` share the same `callback_id`.

**Gap 2 — `delivered_ids` is computed but never used.**

`validate_payload` computes `delivered_ids` from past payloads at line 369 but never compares the current payload's callback IDs against it: [3](#0-2) 

This means cross-block duplicates are also unguarded, but the intra-payload case is the more direct exploit.

**Gap 3 — `push_response` is not idempotent.**

For `GetSuccessorsResponse`, the context is fetched with `get_mut` (not `remove`), so it survives the first call and the second call also succeeds: [4](#0-3) 

For `SendTransactionResponse`, `GetSuccessorsReject`, and `SendTransactionReject`, the context is not looked up at all — each call unconditionally pushes a new entry to `consensus_queue`: [5](#0-4) 

**Gap 4 — `demux` iterates all responses without deduplication.**

`DemuxImpl::process_payload` iterates `batch_messages.bitcoin_adapter_responses` and calls `push_response_bitcoin` for every entry; errors are only logged: [6](#0-5) 

---

### Impact Explanation

A finalized block containing N copies of the same `callback_id` causes N `ConsensusResponse` entries to be pushed into `consensus_queue` for a single pending request. The bitcoin canister's state machine receives N responses to one `bitcoin_get_successors` or `bitcoin_send_transaction` call. For `GetSuccessorsResponse`, the follow-up response store is also overwritten N times: [7](#0-6) 

This corrupts the bitcoin canister's internal state. The double-minting claim (ckBTC) depends on the bitcoin canister's deduplication logic for UTXO processing, which is not analyzed here, but state machine corruption is directly demonstrable.

---

### Likelihood Explanation

Requires a single Byzantine subnet node to act as block proposer in a round where a bitcoin request is pending. This is within the "protocol peer behavior below the consensus fault threshold" attacker model. No privileged access, no key material, and no threshold majority is needed. The precondition (at least one pending `bitcoin_get_successors_contexts` or `bitcoin_send_transaction_internal_contexts` entry) is routinely satisfied on the Bitcoin-enabled subnet.

---

### Recommendation

In `validate_payload`, after decoding the payload, collect all `callback_id` values into a `BTreeSet` and reject the payload if any duplicate is found:

```rust
let mut seen_ids = BTreeSet::new();
for response in &payload {
    if !seen_ids.insert(response.callback_id) {
        return Err(ValidationError::InvalidArtifact(
            consensus::InvalidPayloadReason::InvalidSelfValidatingPayload(
                InvalidSelfValidatingPayloadReason::DuplicateCallbackId,
            ),
        ));
    }
}
```

Additionally, use the already-computed `delivered_ids` to reject callback IDs that were already delivered in past payloads, and consider making `push_response` for `GetSuccessorsResponse` use `remove` instead of `get_mut` so it is idempotent by construction. [8](#0-7) 

---

### Proof of Concept

State-machine test outline:

1. Push one `BitcoinGetSuccessorsContext` into `subnet_call_context_manager` (callback_id = 0).
2. Serialize two identical `BitcoinAdapterResponse { callback_id: 0, response: GetSuccessorsReject(...) }` entries using `iterator_to_bytes`.
3. Call `BitcoinPayloadBuilder::validate_payload` with this crafted byte slice and an empty `past_payloads` slice.
4. Assert the call returns `Ok(())` — demonstrating the missing guard.
5. Call `push_response_bitcoin` twice with the same response.
6. Assert `state.consensus_queue.len() == 2` — demonstrating non-idempotency. [9](#0-8) [10](#0-9)

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

**File:** rs/bitcoin/consensus/src/payload_builder.rs (L357-397)
```rust
    fn validate_payload(
        &self,
        height: Height,
        proposal_context: &ProposalContext,
        payload: &[u8],
        past_payloads: &[PastPayload],
    ) -> Result<(), PayloadValidationError> {
        if payload.is_empty() {
            return Ok(());
        }
        let raw_payload_len = payload.len();

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

        if raw_payload_len as u64 > MAX_BITCOIN_PAYLOAD_IN_BYTES {
            if num_responses == 1 {
                warn!(self.log, "Bitcoin Payload oversized");
            } else {
                return Err(ValidationError::InvalidArtifact(
                    consensus::InvalidPayloadReason::InvalidSelfValidatingPayload(
                        InvalidSelfValidatingPayloadReason::PayloadTooBig,
                    ),
                ));
            }
        }

        Ok(())
    }
```

**File:** rs/replicated_state/src/bitcoin.rs (L23-103)
```rust
pub fn push_response(
    state: &mut ReplicatedState,
    response: BitcoinAdapterResponse,
) -> Result<(), StateError> {
    match response.response {
        BitcoinAdapterResponseWrapper::GetSuccessorsResponse(r) => {
            // Received a response to a request from the bitcoin wasm canister.
            // Retrieve the associated request.
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
        }
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

**File:** rs/bitcoin/consensus/src/payload_builder/parse.rs (L16-27)
```rust
pub(crate) fn bytes_to_payload(
    data: &[u8],
) -> Result<Vec<BitcoinAdapterResponse>, ProxyDecodeError> {
    let messages: Vec<PbBitcoinAdapterResponse> =
        slice_to_messages(data).map_err(ProxyDecodeError::DecodeError)?;

    let messages = messages
        .into_iter()
        .map(|message| message.try_into())
        .collect::<Result<Vec<_>, _>>()?;
    Ok(messages)
}
```
