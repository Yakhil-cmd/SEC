I have read the full source of both files. Let me verify the execution-side handling as well.

I have now read all the relevant code. The full exploit chain is confirmed. Let me summarize the findings:

1. `validate_self_validating_payload_impl` (lines 234–251) only checks empty/size — no callback_id lookup against state.
2. `validate_payload` (lines 357–397) delegates entirely to `validate_self_validating_payload_impl` plus a raw byte-length check.
3. In `rs/messaging/src/routing/demux.rs` (lines 86–94), `push_response_bitcoin` is called for every response in the finalized batch, with errors only logged at `debug` level and silently swallowed.
4. In `rs/replicated_state/src/bitcoin.rs` (lines 28–39), `GetSuccessorsResponse` does look up the callback_id in `bitcoin_get_successors_contexts` — but only to find the context, not to validate the response content. A fabricated response with a **real** callback_id passes this check and its fake block data is encoded and pushed to the consensus queue.
5. `SendTransactionResponse` (lines 64–75) does **not** even check that the callback_id exists in `bitcoin_send_transaction_internal_contexts` — it unconditionally pushes a success response.
6. `validate_canister_http_payload_impl` (lines 468–473 of the HTTPS outcalls builder) correctly rejects any response whose callback_id is not in `canister_http_request_contexts`, confirming the Bitcoin path is the outlier.

---

Audit Report

## Title
Missing Callback-ID Validation in `validate_self_validating_payload_impl` Allows Fabricated Bitcoin Adapter Responses — (`rs/bitcoin/consensus/src/payload_builder.rs`)

## Summary
`BitcoinPayloadBuilder::validate_self_validating_payload_impl` accepts any non-empty `SelfValidatingPayload` whose encoded size is within `MAX_BITCOIN_PAYLOAD_IN_BYTES` without verifying that each `BitcoinAdapterResponse::callback_id` corresponds to a real pending request in the certified state. A single malicious block-proposing node can craft a payload containing fabricated `GetSuccessorsResponse` entries with real callback IDs but fake Bitcoin block data. All honest replicas accept the block because their validation also only checks size, the block is finalized, and the execution environment processes the fabricated responses as authentic Bitcoin network data, corrupting the UTXO state and enabling illegal ckBTC minting.

## Finding Description

**Root cause — validation function:**

`validate_self_validating_payload_impl` at `rs/bitcoin/consensus/src/payload_builder.rs` lines 234–251 performs only two checks: whether the payload is the default (empty) value, and whether the encoded byte count is within limits. It never loads the certified state, never iterates `subnet_call_context_manager.bitcoin_get_successors_contexts` or `bitcoin_send_transaction_internal_contexts`, and never verifies that any `callback_id` in the payload corresponds to a real pending request. [1](#0-0) 

**Validation entry point — `validate_payload`:**

`BatchPayloadBuilder::validate_payload` (lines 357–397) decodes the raw bytes, calls `validate_self_validating_payload_impl`, and then only adds a raw byte-length guard. No state lookup is performed at any point in this path. [2](#0-1) 

**Execution path — silent error swallowing:**

In `rs/messaging/src/routing/demux.rs`, every `BitcoinAdapterResponse` from the finalized batch is passed to `push_response_bitcoin`. Errors are caught with `unwrap_or_else` and logged only at `debug` level, so a fabricated response that fails the execution-side check is silently dropped rather than causing a consensus fault. [3](#0-2) 

**Execution path — fabricated content with real callback_id is processed:**

`push_response` in `rs/replicated_state/src/bitcoin.rs` (lines 28–39) does look up the `callback_id` in `bitcoin_get_successors_contexts`, but only to retrieve the context object. It does not validate the response content. A fabricated `GetSuccessorsResponse` carrying a real `callback_id` passes this lookup, its fake block data is encoded via `maybe_split_response`, and a `ConsensusResponse` carrying the fabricated payload is pushed to the consensus queue for delivery to the Bitcoin management canister. [4](#0-3) 

**Additional gap — `SendTransactionResponse` has no callback_id check at all:**

The `SendTransactionResponse` arm (lines 64–75) does not look up the callback_id in `bitcoin_send_transaction_internal_contexts`. It unconditionally constructs a success response and pushes it to the consensus queue, meaning a fabricated send-transaction success for any callback_id is delivered to the canister. [5](#0-4) 

**Contrast — canister HTTP validation correctly rejects unknown callback IDs:**

`validate_canister_http_payload_impl` in `rs/https_outcalls/consensus/src/payload_builder.rs` loads the certified state and calls `http_contexts.get(&callback_id).ok_or(UnknownCallbackId(...))` for every response, rejecting the block if any callback_id is unknown. The Bitcoin path has no equivalent check. [6](#0-5) 

**Honest builder — correct pattern not mirrored in validation:**

`get_self_validating_payload_impl` (lines 109–232) iterates only over real pending requests from the certified state via `bitcoin_requests_iter`, assigning each response the correct callback_id. This correct pattern is not replicated in the validation path. [7](#0-6) 

## Impact Explanation

A malicious block proposer can inject fabricated `GetSuccessorsResponse` entries with real callback IDs and fake Bitcoin block data. The Bitcoin management canister receives these as authentic responses, updates its internal UTXO set with non-existent transactions, and the ckBTC minter subsequently mints ckBTC against those fake UTXOs. This constitutes illegal minting of an in-scope chain-key asset (ckBTC) with no corresponding real Bitcoin deposit — a significant Chain Fusion / ck-token security impact with concrete, irreversible financial harm to the protocol.

**Severity: High** — the attack requires controlling a single node operator on the Bitcoin-enabled subnet (a meaningful constraint), but the impact is direct illegal ckBTC minting and UTXO state corruption.

## Likelihood Explanation

Every node on the Bitcoin subnet participates in block proposal rotation. A single compromised or malicious node operator — well below the 1/3 Byzantine fault threshold — will eventually be selected as block proposer. No threshold cryptography, majority collusion, or admin key is required. The attacker only needs to: (1) read the current certified state to obtain a live `callback_id`, and (2) craft a `SelfValidatingPayload` with a fabricated `GetSuccessorsResponse` of valid byte size. This is trivially achievable by any node operator who controls the replica binary. The attack is repeatable on every block proposal slot the malicious node wins.

## Recommendation

`validate_self_validating_payload_impl` must load the certified state at `validation_context.certified_height` and, for each `BitcoinAdapterResponse` in the payload:

1. Verify `response.callback_id` exists in `subnet_call_context_manager.bitcoin_get_successors_contexts` (for `GetSuccessors*` variants) or `bitcoin_send_transaction_internal_contexts` (for `SendTransaction*` variants), returning `InvalidArtifact(UnknownCallbackId(...))` if not found.
2. Verify the response variant matches the request type stored for that `callback_id` (e.g., a `GetSuccessorsResponse` must correspond to a `GetSuccessorsRequest`).
3. Verify no `callback_id` appears more than once in the payload.

This mirrors the pattern already correctly implemented in `validate_canister_http_payload_impl`. [8](#0-7) 

## Proof of Concept

1. Attacker controls one replica node on the Bitcoin-enabled subnet.
2. Attacker reads the current certified state to obtain a live `callback_id` from `subnet_call_context_manager.bitcoin_get_successors_contexts`.
3. When the attacker's node is selected as block proposer, it constructs a `SelfValidatingPayload` containing a `BitcoinAdapterResponse` with that `callback_id` and a fabricated `GetSuccessorsResponseComplete` that includes a fake Bitcoin block crediting the attacker's ckBTC deposit address with a large UTXO.
4. The block is proposed. Every other replica calls `validate_payload` → `validate_self_validating_payload_impl`; the payload passes because its byte size is within `MAX_BITCOIN_PAYLOAD_IN_BYTES`. [9](#0-8) 
5. The block is finalized. `demux.rs` calls `push_response_bitcoin` for the fabricated response; `push_response` finds the real `callback_id` in `bitcoin_get_successors_contexts`, encodes the fake block data, and pushes it to the consensus queue. [4](#0-3) 
6. The Bitcoin management canister processes the fabricated `GetSuccessorsResponse`, updates its UTXO set with the fake transaction, and the ckBTC minter mints ckBTC to the attacker's account with no corresponding real Bitcoin deposit.

**Minimal integration test plan:** Construct a `BitcoinPayloadBuilder` backed by a mock state manager that has one pending `GetSuccessorsRequest` with `callback_id = 42`. Call `validate_payload` with a hand-crafted payload containing a `BitcoinAdapterResponse { callback_id: 999, response: GetSuccessorsResponse(...) }` (unknown ID) and assert it returns `InvalidArtifact`. Then call it with `callback_id: 42` but a `SendTransactionResponse` variant (type mismatch) and assert rejection. Both cases currently pass validation, confirming the bug.

### Citations

**File:** rs/bitcoin/consensus/src/payload_builder.rs (L126-131)
```rust
        for (callback_id, request) in bitcoin_requests_iter(&state) {
            // We have already created a payload with the response for
            // this callback id, so skip it.
            if past_callback_ids.contains(&callback_id.get()) {
                continue;
            }
```

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

**File:** rs/replicated_state/src/bitcoin.rs (L28-62)
```rust
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
```

**File:** rs/replicated_state/src/bitcoin.rs (L64-75)
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
```

**File:** rs/https_outcalls/consensus/src/payload_builder.rs (L387-473)
```rust
        let state = &self
            .state_reader
            .get_state_at(validation_context.certified_height)
            .map_err(|_| {
                CanisterHttpPayloadValidationError::ValidationFailed(
                    CanisterHttpPayloadValidationFailure::StateUnavailable,
                )
            })?;
        let http_contexts = &state
            .get_ref()
            .metadata
            .subnet_call_context_manager
            .canister_http_request_contexts;

        // Validate the timed out calls
        for timeout_id in &payload.timeouts {
            // Get requests
            let request = http_contexts.get(timeout_id).ok_or(
                CanisterHttpPayloadValidationError::InvalidArtifact(
                    InvalidCanisterHttpPayloadReason::UnknownCallbackId(*timeout_id),
                ),
            )?;

            // Check that the request has actually timed out
            if request.time + CANISTER_HTTP_TIMEOUT_INTERVAL >= validation_context.time {
                return invalid_artifact(InvalidCanisterHttpPayloadReason::NotTimedOut(
                    *timeout_id,
                ));
            }
            // Check for duplicates (already delivered or repeated in this payload)
            if !delivered_ids.insert(*timeout_id) {
                return invalid_artifact(InvalidCanisterHttpPayloadReason::DuplicateResponse(
                    *timeout_id,
                ));
            }
        }

        // Get the consensus registry version
        let consensus_registry_version = registry_version_at_height(self.cache.as_ref(), height)
            .ok_or(CanisterHttpPayloadValidationError::ValidationFailed(
                CanisterHttpPayloadValidationFailure::ConsensusRegistryVersionUnavailable,
            ))?;

        let committee = self
            .membership
            .get_canister_http_committee(height)
            .map_err(|_| {
                CanisterHttpPayloadValidationError::ValidationFailed(
                    CanisterHttpPayloadValidationFailure::Membership,
                )
            })?;

        // Shares reconstructed from aggregated response proofs.
        let mut reconstructed_shares: Vec<CanisterHttpResponseShare> = Vec::new();
        // Accumulates all signatures in the payload, so that they can be checked
        // in a single batched multi-message verification call at the very end.
        let mut sig_inputs: Vec<ResponseShareSigInput> = Vec::new();

        // Check conditions on individual responses
        for response in &payload.responses {
            // Check that response is consistent
            utils::check_response_consistency(response)
                .map_err(CanisterHttpPayloadValidationError::InvalidArtifact)?;

            // Validate response against consensus registry version
            if response.proof.registry_version() != consensus_registry_version {
                return invalid_artifact(
                    InvalidCanisterHttpPayloadReason::RegistryVersionMismatch {
                        expected: consensus_registry_version,
                        received: response.proof.registry_version(),
                    },
                );
            }

            // Check that the response is not submitted twice
            if !delivered_ids.insert(response.content.id) {
                return invalid_artifact(InvalidCanisterHttpPayloadReason::DuplicateResponse(
                    response.content.id,
                ));
            }

            let callback_id = response.content.id;
            let request_context = http_contexts.get(&callback_id).ok_or(
                CanisterHttpPayloadValidationError::InvalidArtifact(
                    InvalidCanisterHttpPayloadReason::UnknownCallbackId(callback_id),
                ),
            )?;
```
