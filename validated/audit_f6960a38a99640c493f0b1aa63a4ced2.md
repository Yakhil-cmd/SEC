Now I have a complete picture of the full execution path. Let me trace it end-to-end.

### Title
Missing Callback-ID Validation in `push_response` / `SendTransactionResponse` Arm Allows Malicious Block Proposer to Inject Spurious Subnet Responses — (`rs/replicated_state/src/bitcoin.rs`)

---

### Summary

The `SendTransactionResponse` arm of `push_response` unconditionally enqueues a `ConsensusResponse` without verifying that the supplied `callback_id` corresponds to any pending `bitcoin_send_transaction_internal` context. Combined with a completely trivial `validate_self_validating_payload_impl` (size-only check), a single malicious block proposer below the consensus fault threshold can inject a forged response that the execution environment delivers to the Bitcoin canister, causing it to treat a pending `send_transaction` as confirmed even though the underlying Bitcoin transaction was never broadcast.

---

### Finding Description

**Gap 1 — `push_response` asymmetry**

`GetSuccessorsResponse` validates the callback ID before proceeding: [1](#0-0) 

`SendTransactionResponse` (and both `Reject` variants) perform **no such check**: [2](#0-1) 

Any `callback_id` value — including one with no matching context — is unconditionally wrapped in a `ConsensusResponse` and pushed to `state.consensus_queue`.

**Gap 2 — No-op payload validation**

`validate_self_validating_payload_impl` only checks whether the payload is empty: [3](#0-2) 

It never checks whether the `callback_id` values inside the payload correspond to any pending context in the replicated state. A non-empty payload with an arbitrary `callback_id` passes validation unconditionally. This is the gate that honest nodes use when deciding whether to notarize a block proposal.

**Gap 3 — Execution environment delivers the forged response**

The scheduler drains the entire `consensus_queue` each round: [4](#0-3) 

Each entry is passed to `execute_subnet_message` as a `SubnetMessage::Response`. The execution environment calls `retrieve_context`, which searches **all** subnet call context maps: [5](#0-4) 

If the `callback_id` matches any pending context (including `bitcoin_send_transaction_internal_contexts`), the context is **removed** from the map and the forged payload is forwarded to the original requester: [6](#0-5) 

If the `callback_id` matches nothing, the response is silently dropped (line 704). This is the only existing mitigation — and it is defeated whenever the attacker uses a real, observable callback ID.

---

### Impact Explanation

The replicated state is public and certified. A malicious block proposer can read the current `bitcoin_send_transaction_internal_contexts` map, extract a live `callback_id` N, and craft a `SelfValidatingPayload` containing `SendTransactionResponse { callback_id: N }`. After the block is finalized:

1. `push_response` pushes `ConsensusResponse(N, EmptyBlob)` to the queue.
2. The scheduler delivers it to `execute_subnet_message`.
3. `retrieve_context(N)` finds and **removes** the `BitcoinSendTransactionInternal` context.
4. `push_subnet_output_response` delivers `EmptyBlob` to the Bitcoin canister — the expected success payload for `send_transaction`.
5. The Bitcoin canister (and by extension the ckBTC minter) advances its state machine as if the Bitcoin transaction was successfully broadcast, even though it was never sent to the Bitcoin network.

Consequences include: the ckBTC minter releasing ckBTC for a transaction that was never broadcast (enabling double-spend), or permanently losing track of a pending transaction (funds locked or minted without on-chain backing). The same `retrieve_context` chain also covers `sign_with_threshold_contexts`, meaning a fabricated `callback_id` that collides with a pending threshold-signing context would deliver a forged empty-blob reply to a signing canister.

---

### Likelihood Explanation

- The attacker must be a block proposer on the Bitcoin subnet. This requires NNS-approved subnet membership, but the IC threat model explicitly tolerates up to f < n/3 malicious nodes, and the scope rules include "protocol peer behavior below the consensus fault threshold."
- The attack requires only one malicious block proposer turn — no coordination with other nodes.
- The `callback_id` values are observable from certified state reads.
- The validation path (`validate_self_validating_payload_impl`) is a confirmed no-op for non-empty payloads, so honest nodes will notarize the malicious block.
- The attack is fully local-testable with a state-machine test as described in the question.

---

### Recommendation

1. **`push_response` — add context lookup for `SendTransactionResponse`** (and both `Reject` variants) mirroring the `GetSuccessorsResponse` arm: look up the `callback_id` in `bitcoin_send_transaction_internal_contexts` and return `StateError::BitcoinNonMatchingResponse` if absent.

2. **`validate_self_validating_payload_impl` — validate callback IDs against state**: load the certified state at `validation_context.certified_height` and reject any response whose `callback_id` is not present in the corresponding context map (same logic already used in `get_self_validating_payload_impl` via `bitcoin_requests_iter`).

---

### Proof of Concept

```rust
// State-machine test sketch
let mut state = ReplicatedState::new(SUBNET_ID, SubnetType::Application);
// No bitcoin_send_transaction_internal_contexts registered.

let result = push_response(
    &mut state,
    BitcoinAdapterResponse {
        response: BitcoinAdapterResponseWrapper::SendTransactionResponse(
            SendTransactionResponse {},
        ),
        callback_id: u64::MAX,
    },
);

// Bug: returns Ok(()) instead of Err(BitcoinNonMatchingResponse)
assert!(result.is_ok());
// Bug: consensus_queue now contains a spurious response
assert_eq!(state.consensus_queue.len(), 1);
assert_eq!(state.consensus_queue[0].callback.get(), u64::MAX);
```

Contrast with `GetSuccessorsResponse` under identical conditions, which returns `Err(StateError::BitcoinNonMatchingResponse { callback_id: u64::MAX })` and leaves the queue empty. [7](#0-6) [2](#0-1) [3](#0-2) [8](#0-7) [9](#0-8) [10](#0-9)

### Citations

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

**File:** rs/execution_environment/src/scheduler.rs (L1299-1325)
```rust
            while let Some(response) = state.consensus_queue.pop() {
                let (new_state, _) = self.execute_subnet_message(
                    // Wrap the callback ID and payload into a Response, to make it easier for
                    // `execute_subnet_message()` to deal with. All other fields will be ignored by
                    // `execute_subnet_message()`.
                    SubnetMessage::Response(
                        Response {
                            originator: CanisterId::ic_00(),
                            respondent: CanisterId::ic_00(),
                            originator_reply_callback: response.callback,
                            refund: Cycles::zero(),
                            response_payload: response.payload,
                            deadline: NO_DEADLINE,
                        }
                        .into(),
                    ),
                    state,
                    &mut csprng,
                    current_round,
                    &mut subnet_round_limits,
                    registry_settings,
                    replica_version,
                    &measurement_scope,
                    &chain_key_data,
                );
                state = new_state;
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

**File:** rs/execution_environment/src/execution_environment.rs (L698-784)
```rust
            SubnetMessage::Response(response) => {
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
```
