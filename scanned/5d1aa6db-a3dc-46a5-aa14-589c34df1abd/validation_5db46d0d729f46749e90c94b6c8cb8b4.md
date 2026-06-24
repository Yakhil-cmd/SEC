I have traced the full execution path. Here is my analysis.

## Code Path Traced

**Step 1 — `push_response` for reject arms (no context lookup)** [1](#0-0) 

For `GetSuccessorsReject` and `SendTransactionReject`, `push_response` takes the `callback_id` directly from the response and pushes a `Payload::Reject` `ConsensusResponse` to `consensus_queue` **with no lookup** into `bitcoin_get_successors_contexts` or `bitcoin_send_transaction_internal_contexts`. Compare this to the `GetSuccessorsResponse` arm (lines 32–39), which does a mandatory context lookup and returns `StateError::BitcoinNonMatchingResponse` if the `callback_id` is unknown.

**Step 2 — `validate_self_validating_payload_impl` is trivially weak** [2](#0-1) 

The entire validation body is: if the payload is empty return `Ok(0)`; otherwise return `Ok(byte_size)`. There is **no check** that any `callback_id` in the payload corresponds to a pending request in the state. Both `validate_self_validating_payload` and `validate_payload` (the `BatchPayloadBuilder` impl) delegate to this same function. [3](#0-2) 

**Step 3 — Scheduler drains `consensus_queue` unconditionally** [4](#0-3) 

Every entry in `consensus_queue` is wrapped into a `SubnetMessage::Response` and passed to `execute_subnet_message`.

**Step 4 — `execute_subnet_message` calls `retrieve_context` across ALL context types** [5](#0-4) 

`retrieve_context` searches `setup_initial_dkg_contexts`, `sign_with_threshold_contexts`, `reshare_chain_key_contexts`, `canister_http_request_contexts`, `bitcoin_get_successors_contexts`, and `bitcoin_send_transaction_internal_contexts` in order. [6](#0-5) 

If the `callback_id` matches **any** pending context (not just bitcoin ones), the context is removed and the attacker-controlled `response_payload` is forwarded to the originating canister via `push_subnet_output_response`. If no context matches, the response is silently dropped.

---

## Critical Guard Assessment

The only guard that could stop this is `validate_self_validating_payload_impl`. It does not consult the replicated state at all — it only measures bytes. Honest notarizing nodes will therefore sign a block containing a forged `GetSuccessorsReject` or `SendTransactionReject` with an arbitrary `callback_id`, because the payload passes validation.

---

### Title
Missing callback_id validation in `SelfValidatingPayload` allows a malicious block proposer to forge Bitcoin adapter reject responses and deliver them to any canister — (`rs/replicated_state/src/bitcoin.rs`, `rs/bitcoin/consensus/src/payload_builder.rs`)

### Summary
`validate_self_validating_payload_impl` performs no context lookup, and `push_response` for the `GetSuccessorsReject` / `SendTransactionReject` arms also performs no context lookup. A single malicious block proposer can include a crafted `BitcoinAdapterResponse` with either reject variant and an arbitrary `callback_id` in a `SelfValidatingPayload`. Honest nodes will notarize and finalize the block because validation only checks byte size. The forged `ConsensusResponse` is then delivered to execution, which forwards the attacker-controlled reject payload to whichever canister owns that `callback_id`.

### Finding Description
`validate_self_validating_payload_impl` in `rs/bitcoin/consensus/src/payload_builder.rs` (lines 234–251) accepts any non-empty `SelfValidatingPayload` as valid as long as it fits within the byte limit. It does not verify that each `BitcoinAdapterResponse.callback_id` corresponds to a pending entry in `bitcoin_get_successors_contexts` or `bitcoin_send_transaction_internal_contexts`.

`push_response` in `rs/replicated_state/src/bitcoin.rs` (lines 77–102) for the `GetSuccessorsReject` and `SendTransactionReject` arms pushes a `ConsensusResponse` to `consensus_queue` without any context lookup. (The `GetSuccessorsResponse` arm at lines 32–39 correctly performs a lookup and returns `StateError::BitcoinNonMatchingResponse` on mismatch — the reject arms lack this guard.)

The scheduler drains `consensus_queue` unconditionally (lines 1299–1325 of `rs/execution_environment/src/scheduler.rs`). `execute_subnet_message` calls `retrieve_context`, which searches all six context maps. If the attacker-chosen `callback_id` matches any pending context — bitcoin, threshold signing, canister HTTP, DKG, etc. — that context is consumed and the attacker-controlled `Payload::Reject` is forwarded to the originating canister.

### Impact Explanation
- Any canister on a bitcoin-enabled subnet that has a pending `BitcoinGetSuccessors` or `BitcoinSendTransactionInternal` call can have its reject handler triggered with an attacker-chosen `RejectCode` and message string.
- Because `retrieve_context` searches all context types, the same technique can forge rejections for threshold-signing (`SignWithECDSA`/`SignWithSchnorr`) or canister-HTTP contexts whose `callback_id` the attacker can observe from the replicated state.
- The consumed context is permanently removed; the legitimate adapter response that eventually arrives is silently dropped (for success variants) or pushed again without effect (for reject variants), causing the pending call to never complete legitimately.

### Likelihood Explanation
A single malicious subnet node (below the Byzantine fault threshold) can propose such a block in any round it is selected as block maker. `callback_id` values are sequential and observable from the public replicated state. The attack is deterministic and requires no brute-force.

### Recommendation
1. In `push_response`, add a context lookup for `GetSuccessorsReject` and `SendTransactionReject` identical to the one already present for `GetSuccessorsResponse` — return `StateError::BitcoinNonMatchingResponse` if the `callback_id` is not found.
2. In `validate_self_validating_payload_impl`, load the certified state and verify that each `callback_id` in the payload corresponds to a pending entry in the appropriate context map, and that the response variant matches the request type.

### Proof of Concept
State-machine test outline:
1. Create a canister that issues a `BitcoinGetSuccessors` call; record the assigned `callback_id` (e.g., `0`).
2. Directly push a `BitcoinAdapterResponse { response: GetSuccessorsReject(BitcoinReject { reject_code: SysFatal, message: "forged" }), callback_id: 0 }` into the `SelfValidatingPayload` of a block (bypassing the builder, or using a patched builder that skips the adapter call).
3. Call `validate_payload` — assert it returns `Ok(())`.
4. Deliver the batch; tick the state machine.
5. Assert the canister's reject handler fired with `reject_code = SysFatal` and `message = "forged"`.
6. Assert the `bitcoin_get_successors_contexts` map is now empty (context was consumed).

### Citations

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

**File:** rs/execution_environment/src/execution_environment.rs (L698-785)
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
                };
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
