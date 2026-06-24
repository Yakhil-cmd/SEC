Audit Report

## Title
Missing Callback ID Validation in `push_response` `SendTransactionResponse`/Reject Arms Allows Byzantine Block Proposer to Inject Spurious `ConsensusResponse` — (`rs/replicated_state/src/bitcoin.rs`)

## Summary

The `SendTransactionResponse`, `GetSuccessorsReject`, and `SendTransactionReject` arms of `push_response` unconditionally push a `ConsensusResponse` with an attacker-controlled `callback_id` into `consensus_queue`, without verifying the ID exists in any context map. The Bitcoin payload validator (`validate_self_validating_payload_impl`) performs only a size check, so a Byzantine block proposer can craft a `SelfValidatingPayload` that passes all validation, gets finalized, and injects a spurious `ConsensusResponse` into the subnet's `consensus_queue`. Because all subnet call context types (`SignWithThreshold`, `CanisterHttpRequest`, `BitcoinGetSuccessors`, etc.) share a single sequential `next_callback_id` counter, a targeted injection can prematurely resolve and consume a pending threshold-signing or HTTP-outcall context with `EmptyBlob`.

## Finding Description

**Asymmetric validation in `push_response`:**

The `GetSuccessorsResponse` arm validates the callback ID against `bitcoin_get_successors_contexts` and returns `Err(StateError::BitcoinNonMatchingResponse)` if not found: [1](#0-0) 

The `SendTransactionResponse`, `GetSuccessorsReject`, and `SendTransactionReject` arms perform no such check — they unconditionally push to `consensus_queue`: [2](#0-1) [3](#0-2) 

**Payload validation is a size-only no-op:**

`validate_self_validating_payload_impl` returns `Ok(size)` after only checking for empty payload and byte count. It performs zero validation of callback IDs against replicated state: [4](#0-3) 

This is called by both `validate_self_validating_payload` and `validate_payload` (the `BatchPayloadBuilder` path), so every honest node will accept and notarize a block containing a crafted `BitcoinAdapterResponse{SendTransactionResponse, callback_id: X}` for any arbitrary `X`.

**`consensus_queue` is fully drained each round (fatal assertion):** [5](#0-4) 

**`retrieve_context` searches across ALL context types from a shared counter:**

All context types — `SetupInitialDKG`, `SignWithThreshold`, `ReshareChainKey`, `CanisterHttpRequest`, `BitcoinGetSuccessors`, `BitcoinSendTransactionInternal` — share a single monotonically increasing `next_callback_id` counter: [6](#0-5) 

`retrieve_context` searches all maps in sequence using the same `callback_id`: [7](#0-6) 

The execution environment calls `retrieve_context` for every `SubnetMessage::Response` drained from `consensus_queue`: [8](#0-7) 

If the injected `callback_id` matches a pending `SignWithThreshold` context, that context is removed and resolved with `EmptyBlob`. The legitimate threshold-signing response, when it arrives, finds no matching context and is silently dropped. [9](#0-8) 

**Contrast with HTTP outcalls validation**, which correctly rejects unknown callback IDs: [10](#0-9) 

## Impact Explanation

A Byzantine block proposer on the Bitcoin integration subnet (where Bitcoin integration is enabled) can observe sequential `callback_id` values from certified state and craft a `SelfValidatingPayload` targeting a pending `SignWithThreshold` context (used by ckBTC, ckETH, and other chain-key operations). The injected `ConsensusResponse` prematurely resolves the context with `EmptyBlob`, delivering malformed data to the requesting canister and permanently consuming the context so the legitimate threshold-signing response is dropped. This constitutes a **High** severity impact: significant Chain Fusion / ck-token security impact with concrete user and protocol harm, constrained to subnets with Bitcoin integration enabled and requiring a single Byzantine block proposer.

## Likelihood Explanation

- Bitcoin integration must be enabled on the subnet (it is on the production Bitcoin integration subnet).
- The attacker must be a subnet node acting as block proposer — a single Byzantine node below the `f < n/3` consensus fault threshold, which is a valid attacker model per the ICP bounty scope.
- `callback_id` values are sequential integers observable from certified state, making targeted injection of a known pending context feasible.
- No threshold of nodes needs to be corrupted; the size-only payload validation means every honest node will notarize and finalize the crafted block.
- The attack is repeatable every block the Byzantine node proposes.

## Recommendation

In the `SendTransactionResponse`, `GetSuccessorsReject`, and `SendTransactionReject` arms of `push_response`, add the same guard present in the `GetSuccessorsResponse` arm: look up the `callback_id` in `bitcoin_send_transaction_internal_contexts` (for send-transaction variants) or `bitcoin_get_successors_contexts` (for get-successors reject), and return `Err(StateError::BitcoinNonMatchingResponse { callback_id: callback_id.get() })` if not found.

Additionally, `validate_self_validating_payload_impl` should validate that each `callback_id` in the payload corresponds to an existing pending context in the certified state (via `bitcoin_requests_iter` or direct map lookup), mirroring how `rs/https_outcalls/consensus/src/payload_builder.rs` validates `http_contexts.get(&callback_id)`.

## Proof of Concept

```rust
// Unit test for push_response in rs/replicated_state/src/bitcoin.rs
// Demonstrates that SendTransactionResponse with unknown callback_id succeeds
// and injects a spurious ConsensusResponse.

let mut state = ReplicatedState::new(SUBNET_ID, SubnetType::Application);
// bitcoin_send_transaction_internal_contexts is empty — no pending context.
let result = push_response(
    &mut state,
    BitcoinAdapterResponse {
        response: BitcoinAdapterResponseWrapper::SendTransactionResponse(
            SendTransactionResponse {}
        ),
        callback_id: 999, // arbitrary, not in any context map
    },
);
assert!(result.is_ok());           // passes — no validation performed
assert_eq!(state.consensus_queue.len(), 1); // spurious entry injected
assert_eq!(
    state.consensus_queue[0].originator_reply_callback,
    CallbackId::from(999)
);
// If callback_id 999 matches a pending SignWithThreshold context,
// retrieve_context will remove and resolve it with EmptyBlob.
```

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

**File:** rs/replicated_state/src/metadata_state/subnet_call_context_manager.rs (L213-230)
```rust
pub struct SubnetCallContextManager {
    /// Should increase monotonically. This property is used to determine if a request
    /// corresponds to a future state.
    next_callback_id: u64,
    pub setup_initial_dkg_contexts: BTreeMap<CallbackId, SetupInitialDkgContext>,
    pub sign_with_threshold_contexts: BTreeMap<CallbackId, SignWithThresholdContext>,
    pub canister_http_request_contexts: BTreeMap<CallbackId, CanisterHttpRequestContext>,
    /// `CanisterHttpRequestContext`s whose responses have already been delivered to execution.
    /// They are kept here such that asynchronous refunds may continue to be processed.
    pub delivered_canister_http_request_contexts: BTreeMap<CallbackId, CanisterHttpRequestContext>,
    pub reshare_chain_key_contexts: BTreeMap<CallbackId, ReshareChainKeyContext>,
    pub bitcoin_get_successors_contexts: BTreeMap<CallbackId, BitcoinGetSuccessorsContext>,
    pub bitcoin_send_transaction_internal_contexts:
        BTreeMap<CallbackId, BitcoinSendTransactionInternalContext>,
    canister_management_calls: CanisterManagementCalls,
    pub raw_rand_contexts: VecDeque<RawRandContext>,
    pub pre_signature_stashes: BTreeMap<IDkgMasterPublicKeyId, PreSignatureStash>,
}
```

**File:** rs/replicated_state/src/metadata_state/subnet_call_context_manager.rs (L269-350)
```rust
    pub fn retrieve_context(
        &mut self,
        callback_id: CallbackId,
        logger: &ReplicaLogger,
    ) -> Option<SubnetCallContext> {
        self.setup_initial_dkg_contexts
            .remove(&callback_id)
            .map(|context| {
                info!(
                    logger,
                    "Received the response for SetupInitialDKG request for target {:?}",
                    context.target_id
                );
                SubnetCallContext::SetupInitialDKG(context)
            })
            .or_else(|| {
                self.sign_with_threshold_contexts
                    .remove(&callback_id)
                    .map(|context| {
                        info!(
                            logger,
                            "Received the response for SignWithThreshold request with id {:?} from {:?}",
                            callback_id,
                            context.request.sender
                        );
                        SubnetCallContext::SignWithThreshold(context)
                    })
            })
            .or_else(|| {
                self.reshare_chain_key_contexts
                    .remove(&callback_id)
                    .map(|context| {
                        info!(
                            logger,
                            "Received the response for ReshareChainKey request with key_id {:?} and callback id {:?} from {:?}",
                            context.key_id,
                            context.request.sender_reply_callback,
                            context.request.sender
                        );
                        SubnetCallContext::ReshareChainKey(context)
                    })
            })
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
            .or_else(|| {
                self.bitcoin_get_successors_contexts
                    .remove(&callback_id)
                    .map(|context| {
                        info!(
                            logger,
                            "Received the response for BitcoinGetSuccessors with callback id {:?} from {:?}",
                            context.request.sender_reply_callback,
                            context.request.sender
                        );
                        SubnetCallContext::BitcoinGetSuccessors(context)
                    })
            })
            .or_else(|| {
                self.bitcoin_send_transaction_internal_contexts
                    .remove(&callback_id)
                    .map(|context| {
                        info!(
                            logger,
                            "Received the response for BitcoinSendTransactionInternal with callback id {:?} from {:?}",
                            context.request.sender_reply_callback,
                            context.request.sender
                        );
                        SubnetCallContext::BitcoinSendTransactionInternal(context)
                    })
            })
    }
```

**File:** rs/execution_environment/src/execution_environment.rs (L698-705)
```rust
            SubnetMessage::Response(response) => {
                let context = state
                    .metadata
                    .subnet_call_context_manager
                    .retrieve_context(response.originator_reply_callback, &self.log);
                return match context {
                    None => (state, ExecuteSubnetMessageResultType::Finished),
                    Some(context) => {
```

**File:** rs/execution_environment/src/execution_environment.rs (L758-781)
```rust
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
