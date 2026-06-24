Audit Report

## Title
Byzantine Block Proposer Can Deliver `EmptyBlob` to a `GetSuccessors` Callback via Unvalidated `SendTransactionResponse` — (`rs/bitcoin/consensus/src/payload_builder.rs`, `rs/replicated_state/src/bitcoin.rs`)

## Summary
`validate_self_validating_payload_impl` performs no content validation on non-empty `SelfValidatingPayload`s, accepting any response regardless of whether the `callback_id` or response variant is valid. The `SendTransactionResponse` branch in `push_response` does not verify the `callback_id` against `bitcoin_send_transaction_internal_contexts` and unconditionally pushes `EmptyBlob` to `consensus_queue`. A Byzantine block proposer can craft a payload with `SendTransactionResponse(callback_id=X)` where `X` belongs to a `GetSuccessors` context, causing the bitcoin canister's `GetSuccessors` callback to fire with `EmptyBlob` instead of a `BitcoinGetSuccessorsResponse`, disrupting ckBTC UTXO sync.

## Finding Description

**Root cause 1 — no-op validator:**

`validate_self_validating_payload_impl` at lines 234–251 of `rs/bitcoin/consensus/src/payload_builder.rs` returns `Ok(size)` for any non-empty payload without reading state, checking that `callback_id` values exist in any context map, or verifying that the response variant matches the request type stored in state. [1](#0-0) 

**Root cause 2 — `SendTransactionResponse` skips all state validation:**

The `SendTransactionResponse` branch in `push_response` (lines 64–76 of `rs/replicated_state/src/bitcoin.rs`) does not look up `callback_id` in `bitcoin_send_transaction_internal_contexts`. It unconditionally constructs `Payload::Data(EmptyBlob.encode())` and pushes a `ConsensusResponse` for whatever `callback_id` was provided, returning `Ok(())`. [2](#0-1) 

Contrast with `GetSuccessorsResponse` (lines 28–39), which calls `.get_mut(&callback_id).ok_or_else(|| StateError::BitcoinNonMatchingResponse {...})?` and returns an error if the `callback_id` is absent from `bitcoin_get_successors_contexts`. [3](#0-2) 

**Attack sequence:**
1. Attacker observes `bitcoin_get_successors_contexts` contains `callback_id=X` (visible from certified state).
2. Byzantine block proposer crafts `SelfValidatingPayload { responses: [BitcoinAdapterResponse { callback_id: X, response: SendTransactionResponse({}) }] }`.
3. All honest validators call `validate_self_validating_payload_impl` → passes unconditionally.
4. Block finalizes; `process_payload` calls `push_response_bitcoin` deterministically on all nodes.
5. `push_response` matches `SendTransactionResponse` branch → pushes `ConsensusResponse(callback_id=X, payload=EmptyBlob)` to `consensus_queue`.
6. The bitcoin canister's `GetSuccessors` callback fires with `EmptyBlob`; Candid decode of `BitcoinGetSuccessorsResponse` fails.

The `demux.rs` call site swallows errors from `push_response_bitcoin` at debug level only, but in this case `push_response` returns `Ok(())` regardless, so no error is surfaced. [4](#0-3) 

Note: `GetSuccessorsReject` and `SendTransactionReject` branches (lines 77–102) also lack `callback_id` validation, making them susceptible to the same cross-type injection. [5](#0-4) 

## Impact Explanation
The bitcoin canister is the core Chain Fusion component managing UTXO state for ckBTC. Its `GetSuccessors` callback expects a Candid-encoded `BitcoinGetSuccessorsResponse`; receiving `EmptyBlob` causes a decode failure. This disrupts UTXO synchronization and can leave the bitcoin canister's internal state machine in an inconsistent position, constituting a significant Chain Fusion / ck-token security impact with concrete protocol harm. This maps to **High ($2,000–$10,000)**: "Significant Chain Fusion, ck-token, ledger… security impact with concrete user or protocol harm." The speculative claim of false ckBTC minting is not proven by the evidence and is not included in the severity assessment.

## Likelihood Explanation
Requires a single Byzantine block proposer — one node acting maliciously below the BFT fault threshold. The `callback_id` values are observable from certified state. No special keys, governance majority, or external compromise is needed. The attack is repeatable every round a Byzantine node holds the proposer role.

## Recommendation
1. **Fix `validate_self_validating_payload_impl`**: Load state at `validation_context.certified_height` and for each response in the payload, verify that `callback_id` exists in the correct context map (`bitcoin_get_successors_contexts` for `GetSuccessorsResponse`/`GetSuccessorsReject`, `bitcoin_send_transaction_internal_contexts` for `SendTransactionResponse`/`SendTransactionReject`). Return `InvalidArtifact` on any mismatch.
2. **Fix `SendTransactionResponse` in `push_response`**: Add the same `.get(&callback_id).ok_or_else(|| StateError::BitcoinNonMatchingResponse {...})?` guard already present in the `GetSuccessorsResponse` branch, and remove the context from `bitcoin_send_transaction_internal_contexts` on success.
3. **Fix `GetSuccessorsReject` and `SendTransactionReject`**: Apply equivalent `callback_id` validation to these branches as well.

## Proof of Concept
```rust
// Unit test sketch (rs/replicated_state/src/bitcoin.rs test module):
// 1. Build a ReplicatedState with:
//      state.metadata.subnet_call_context_manager
//          .bitcoin_get_successors_contexts
//          .insert(CallbackId(0), <some BitcoinGetSuccessorsContext>)
// 2. Construct:
//      let response = BitcoinAdapterResponse {
//          callback_id: 0,
//          response: BitcoinAdapterResponseWrapper::SendTransactionResponse(
//              SendTransactionResponse {}
//          ),
//      };
// 3. Call push_response(&mut state, response)
//      → assert!(result.is_ok())
// 4. Assert state.consensus_queue contains a ConsensusResponse
//      with callback_id=0 and Payload::Data(EmptyBlob.encode())
// 5. Assert bitcoin_get_successors_contexts still contains CallbackId(0)
//      (context not removed, can be targeted again)
//
// Separately, call validate_self_validating_payload_impl with the same
// payload and assert Ok(_) is returned — confirming no validator rejects it.
```

### Citations

**File:** rs/bitcoin/consensus/src/payload_builder.rs (L241-251)
```rust
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
