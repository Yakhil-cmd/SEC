### Title
Missing Callback ID Validation in `push_response` SendTransactionResponse Arm Allows Byzantine Block Proposer to Inject Arbitrary ConsensusResponse — (`rs/replicated_state/src/bitcoin.rs`)

---

### Summary

The `SendTransactionResponse` arm of `push_response` unconditionally pushes a `ConsensusResponse` with an attacker-controlled `callback_id` into `consensus_queue`, without verifying the ID exists in `bitcoin_send_transaction_internal_contexts`. The payload validation function (`validate_self_validating_payload_impl`) is a size-only no-op that does not check callback IDs against state. A Byzantine block proposer (a single Byzantine subnet node, within the f < n/3 fault tolerance) can craft a `SelfValidatingPayload` that passes all validation, gets finalized, and injects a spurious `ConsensusResponse` into the subnet's `consensus_queue`.

---

### Finding Description

**Root cause — asymmetric validation in `push_response`:**

The `GetSuccessorsResponse` arm validates the callback ID:

```rust
let context = state
    .metadata
    .subnet_call_context_manager
    .bitcoin_get_successors_contexts
    .get_mut(&callback_id)
    .ok_or_else(|| StateError::BitcoinNonMatchingResponse {
        callback_id: callback_id.get(),
    })?;
``` [1](#0-0) 

The `SendTransactionResponse` arm performs **no such check**:

```rust
BitcoinAdapterResponseWrapper::SendTransactionResponse(_) => {
    let callback_id = CallbackId::from(response.callback_id);
    let payload = Payload::Data(EmptyBlob.encode());
    state.consensus_queue.push(ConsensusResponse::new(callback_id, payload));
    Ok(())
}
``` [2](#0-1) 

The same omission applies to `GetSuccessorsReject` and `SendTransactionReject` arms. [3](#0-2) 

**Payload validation is a no-op for callback IDs:**

`validate_self_validating_payload_impl` only checks whether the payload is empty or exceeds the byte limit. It performs zero validation of callback IDs against replicated state:

```rust
fn validate_self_validating_payload_impl(
    &self,
    payload: &SelfValidatingPayload,
    validation_context: &ValidationContext,
) -> Result<NumBytes, SelfValidatingPayloadValidationError> {
    if *payload == SelfValidatingPayload::default() {
        return Ok(0.into());
    }
    // ... only size check follows
    Ok(size)
}
``` [4](#0-3) 

This means every honest node will accept and notarize a block containing a crafted `BitcoinAdapterResponse{SendTransactionResponse, callback_id: X}` for any arbitrary X.

**`consensus_queue` must be fully drained each round:**

After execution, the state machine asserts the queue is empty:

```rust
if !state_after_execution.consensus_queue.is_empty() {
    fatal!(self.log, "Consensus queue not empty at the end of round {:?}.", batch.batch_number)
}
``` [5](#0-4) 

This guarantees the injected entry is processed.

**Processing of the injected entry:**

When the scheduler processes `consensus_queue` entries as `SubnetMessage::Response`, the execution environment calls `retrieve_context(response.originator_reply_callback, ...)`:

```rust
let context = state
    .metadata
    .subnet_call_context_manager
    .retrieve_context(response.originator_reply_callback, &self.log);
return match context {
    None => (state, ExecuteSubnetMessageResultType::Finished),
    Some(context) => { /* delivers response to canister */ }
};
``` [6](#0-5) 

- If `X` is **unknown**: the entry is silently dropped (`None` branch). Impact is minimal.
- If `X` matches a **legitimate pending context** (e.g., a `SignWithECDSA`, `CanisterHttpRequest`, or another Bitcoin context): `retrieve_context` removes and resolves that context with the injected `EmptyBlob` payload. The canister receives an incorrect response, the legitimate pending context is consumed, and the real response (when it eventually arrives) will find no matching context and be dropped.

---

### Impact Explanation

A Byzantine block proposer who knows (or can guess) a valid pending `callback_id` — which are sequential integers visible in the subnet's certified state — can:

1. Prematurely resolve a pending `SignWithECDSA` or `CanisterHttpRequest` context with `EmptyBlob`, causing the target canister's reply callback to receive malformed data.
2. Consume the context so the legitimate response is silently dropped when it arrives.
3. Cause canister-level misbehavior: incorrect reply delivery, Candid decode failures in the reply handler, or loss of the expected result.

Even with an unknown `callback_id`, the attacker can flood the `consensus_queue` with spurious entries (one per block they propose), consuming execution resources each round.

---

### Likelihood Explanation

- Bitcoin integration must be enabled on the subnet (it is on the Bitcoin integration subnet).
- The attacker must be a subnet node acting as block proposer — within the "protocol peer behavior below the consensus fault threshold" entry point.
- `callback_id` values are sequential and observable from certified state, making targeted injection feasible.
- No threshold of nodes needs to be corrupted; a single Byzantine proposer suffices because validation is a size-only check.

---

### Recommendation

In the `SendTransactionResponse`, `GetSuccessorsReject`, and `SendTransactionReject` arms of `push_response`, add the same guard present in the `GetSuccessorsResponse` arm: look up the `callback_id` in the appropriate context map (`bitcoin_send_transaction_internal_contexts` for send-transaction variants) and return `Err(StateError::BitcoinNonMatchingResponse { ... })` if not found.

Additionally, `validate_self_validating_payload_impl` should validate that each `callback_id` in the payload corresponds to an existing pending context in the certified state, mirroring how canister HTTP outcall payload validation checks `http_contexts.get(&callback_id)`. [7](#0-6) 

---

### Proof of Concept

```rust
// In push_response: call with SendTransactionResponse and a callback_id
// not present in bitcoin_send_transaction_internal_contexts.
// Expected: Err(StateError::BitcoinNonMatchingResponse)
// Actual:   Ok(()), with a spurious ConsensusResponse in consensus_queue.

let mut state = ReplicatedState::new(SUBNET_ID, SubnetType::Application);
// No context pushed — bitcoin_send_transaction_internal_contexts is empty.
let result = push_response(
    &mut state,
    BitcoinAdapterResponse {
        response: BitcoinAdapterResponseWrapper::SendTransactionResponse(
            SendTransactionResponse {}
        ),
        callback_id: 999, // arbitrary, not in any context map
    },
);
assert!(result.is_ok()); // passes — no validation
assert_eq!(state.consensus_queue.len(), 1); // spurious entry injected
assert_eq!(state.consensus_queue[0].callback_id, CallbackId::from(999));
``` [2](#0-1)

### Citations

**File:** rs/replicated_state/src/bitcoin.rs (L31-39)
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

**File:** rs/replicated_state/src/bitcoin.rs (L77-102)
```rust
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

**File:** rs/messaging/src/state_machine.rs (L248-254)
```rust
        if !state_after_execution.consensus_queue.is_empty() {
            fatal!(
                self.log,
                "Consensus queue not empty at the end of round {:?}.",
                batch.batch_number
            )
        }
```

**File:** rs/execution_environment/src/execution_environment.rs (L699-785)
```rust
                let context = state
                    .metadata
                    .subnet_call_context_manager
                    .retrieve_context(response.originator_reply_callback, &self.log);
                return match context {
                    None => (state, ExecuteSubnetMessageResultType::Finished),
                    Some(context) => {
                        let time_elapsed =
                            state.time().saturating_duration_since(context.get_time());
                        let request = context.get_request();

                        if let SubnetCallContext::CanisterHttpRequest(context) = &context {
                            let old_price = self.cycles_account_manager.http_request_fee(
                                context.variable_parts_size(),
                                context.max_response_bytes,
                                state.get_own_subnet_cycles_config(),
                            );

                            let new_price = self.cycles_account_manager.http_request_fee_beta(
                                context.variable_parts_size(),
                                context.max_response_bytes,
                                state.get_own_subnet_cycles_config(),
                                NumBytes::from(response.payload_size_bytes()),
                            );

                            self.metrics.observe_http_outcall_price_change(
                                old_price.nominal(),
                                new_price.nominal(),
                            );
                            self.metrics
                                .observe_http_outcall_request(context, &response);

                            let max_response_size = match context.max_response_bytes {
                                Some(response_size) => response_size.get(),
                                // Defaults to maximum response size.
                                None => MAX_CANISTER_HTTP_RESPONSE_BYTES,
                            };

                            info!(
                                self.log,
                                "Canister Http request with payload_size {}, max_response_size {}, subnet_size {}, reply_callback_id {}, sender {}, process_id {}",
                                response.payload_size_bytes().get(),
                                max_response_size,
                                registry_settings.subnet_size,
                                context.request.sender_reply_callback,
                                context.request.sender,
                                std::process::id(),
                            );
                        }

                        self.metrics.observe_subnet_message(
                            &request.method_name,
                            time_elapsed.as_secs_f64(),
                            &match &response.response_payload {
                                Payload::Data(_) => Ok(()),
                                Payload::Reject(_) => Err(ErrorCode::CanisterRejectedMessage),
                            },
                        );

                        if let (
                            SubnetCallContext::SignWithThreshold(threshold_context),
                            Payload::Data(_),
                        ) = (&context, &response.response_payload)
                        {
                            *state
                                .metadata
                                .subnet_metrics
                                .threshold_signature_agreements
                                .entry(threshold_context.key_id())
                                .or_default() += 1;
                        }

                        state.push_subnet_output_response(
                            Response {
                                originator: request.sender,
                                respondent: CanisterId::from(self.own_subnet_id),
                                originator_reply_callback: request.sender_reply_callback,
                                refund: request.payment,
                                response_payload: response.response_payload.clone(),
                                deadline: request.deadline,
                            }
                            .into(),
                        );

                        (state, ExecuteSubnetMessageResultType::Finished)
                    }
                };
```

**File:** rs/https_outcalls/consensus/src/payload_builder.rs (L468-473)
```rust
            let callback_id = response.content.id;
            let request_context = http_contexts.get(&callback_id).ok_or(
                CanisterHttpPayloadValidationError::InvalidArtifact(
                    InvalidCanisterHttpPayloadReason::UnknownCallbackId(callback_id),
                ),
            )?;
```
