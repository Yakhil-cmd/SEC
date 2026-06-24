Looking at the actual code, I need to trace the full path carefully.

**Step 1: `validate_self_validating_payload_impl` — does it check response types?** [1](#0-0) 

The function does nothing beyond checking if the payload is empty. For any non-empty payload it immediately returns the byte size. There is no check that:
- The `callback_id` exists in state
- The response variant matches the request type stored in state

**Step 2: `push_response` — does `SendTransactionResponse` validate the callback_id?** [2](#0-1) 

`SendTransactionResponse` does **not** look up the `callback_id` in `bitcoin_send_transaction_internal_contexts`. It unconditionally pushes `EmptyBlob` to `consensus_queue` with whatever `callback_id` was provided.

Contrast this with `GetSuccessorsResponse`, which **does** validate: [3](#0-2) 

`GetSuccessorsResponse` calls `.ok_or_else(|| StateError::BitcoinNonMatchingResponse {...})?` — it returns an error if the callback_id is not found in `bitcoin_get_successors_contexts`. `SendTransactionResponse` has no equivalent guard.

**Step 3: The `GetSuccessors` context is never removed** [2](#0-1) 

The `SendTransactionResponse` branch never touches `bitcoin_get_successors_contexts` or `bitcoin_send_transaction_internal_contexts`. So after processing, the `GetSuccessors` context at `callback_id=X` remains in state.

**Step 4: Attacker entry point**

The attacker is a single Byzantine block proposer — "protocol peer behavior below the consensus fault threshold" per the scope rules. All honest validators call `validate_self_validating_payload_impl` and accept the block because the function performs no content validation. [4](#0-3) 

After finalization, `push_response_bitcoin` is called deterministically on all nodes.

---

### Title
Byzantine Block Proposer Can Deliver `EmptyBlob` to a `GetSuccessors` Callback via Mismatched `SendTransactionResponse` — (`rs/bitcoin/consensus/src/payload_builder.rs`, `rs/replicated_state/src/bitcoin.rs`)

### Summary
`validate_self_validating_payload_impl` performs no content validation on non-empty `SelfValidatingPayload`s. The `SendTransactionResponse` branch in `push_response` does not verify the `callback_id` against `bitcoin_send_transaction_internal_contexts` and unconditionally pushes `EmptyBlob` to `consensus_queue`. A Byzantine block proposer can craft a payload with `SendTransactionResponse(callback_id=X)` where `X` belongs to a `GetSuccessors` context, causing the bitcoin canister to receive `EmptyBlob` instead of a `GetSuccessorsResponse`.

### Finding Description

**Root cause 1 — no-op validator:** [5](#0-4) 

For any non-empty payload, `validate_self_validating_payload_impl` returns `Ok(size)` immediately. It never reads state, never checks that `callback_id` values exist in the correct context map, and never verifies that the response variant matches the request type.

**Root cause 2 — `SendTransactionResponse` skips all state validation:** [2](#0-1) 

Unlike `GetSuccessorsResponse` (which calls `.get_mut(&callback_id).ok_or_else(...)` and errors on mismatch), `SendTransactionResponse` blindly pushes `EmptyBlob` to `consensus_queue` for any `callback_id`, including ones that belong to `GetSuccessors` contexts.

**Attack sequence:**
1. Attacker observes `bitcoin_get_successors_contexts` contains `callback_id=X` (visible from certified state).
2. Attacker (Byzantine block proposer) proposes a block with `SelfValidatingPayload { responses: [BitcoinAdapterResponse { callback_id: X, response: SendTransactionResponse({}) }] }`.
3. All honest validators call `validate_self_validating_payload_impl` → passes (no content check).
4. Block finalizes; `process_payload` calls `push_response_bitcoin` on all nodes.
5. `push_response` matches `SendTransactionResponse` branch → pushes `ConsensusResponse(callback_id=X, payload=EmptyBlob)` to `consensus_queue`.
6. `bitcoin_get_successors_contexts[X]` is **not removed** — the context leaks.
7. The bitcoin canister's `GetSuccessors` callback fires with `EmptyBlob` payload.

### Impact Explanation
The bitcoin canister expects a `GetSuccessorsResponse` (Candid-encoded `BitcoinGetSuccessorsResponse`) for its `GetSuccessors` callback. Receiving `EmptyBlob` will cause a Candid decode failure in the canister's response handler. Depending on how the bitcoin canister handles this error, consequences range from a stalled UTXO sync (the context remains in state and will be re-queried, but the canister's internal state machine may be left in an inconsistent position) to potential UTXO tracking corruption that could enable false ckBTC minting if the canister's error path does not properly roll back state.

Additionally, the leaked `GetSuccessors` context means the same `callback_id` can be targeted again in subsequent rounds.

### Likelihood Explanation
Requires a single Byzantine block proposer — within the BFT fault tolerance model and explicitly in scope as "protocol peer behavior below the consensus fault threshold." The `callback_id` values are observable from certified state. No special keys, governance majority, or external compromise is needed.

### Recommendation
1. **Fix `validate_self_validating_payload_impl`**: Load state at `validation_context.certified_height` and for each response in the payload, verify that `callback_id` exists in the correct context map (`bitcoin_get_successors_contexts` for `GetSuccessorsResponse`/`GetSuccessorsReject`, `bitcoin_send_transaction_internal_contexts` for `SendTransactionResponse`/`SendTransactionReject`). Reject the payload with `InvalidArtifact` if any mismatch is found.

2. **Fix `SendTransactionResponse` in `push_response`**: Add the same `.get(&callback_id).ok_or_else(|| StateError::BitcoinNonMatchingResponse {...})?` guard that `GetSuccessorsResponse` already has, and remove the context from `bitcoin_send_transaction_internal_contexts` on success.

### Proof of Concept
```rust
// Unit test sketch:
// 1. Build a ReplicatedState with bitcoin_get_successors_contexts[CallbackId(0)] = <some context>
// 2. Construct payload = SelfValidatingPayload::new(vec![BitcoinAdapterResponse {
//        callback_id: 0,
//        response: BitcoinAdapterResponseWrapper::SendTransactionResponse(SendTransactionResponse {}),
//    }])
// 3. Call validate_self_validating_payload_impl(&payload, &ctx) → assert Ok(_)
// 4. Call push_response(&mut state, response)
// 5. Assert state.consensus_queue contains ConsensusResponse with EmptyBlob payload
// 6. Assert state.metadata.subnet_call_context_manager
//        .bitcoin_get_successors_contexts still contains CallbackId(0)  ← leaked context
```

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

**File:** rs/replicated_state/src/bitcoin.rs (L28-39)
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
```

**File:** rs/replicated_state/src/bitcoin.rs (L64-76)
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
