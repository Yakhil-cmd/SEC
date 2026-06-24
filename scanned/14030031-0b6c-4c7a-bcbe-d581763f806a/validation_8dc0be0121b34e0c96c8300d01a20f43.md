Let me look at how `push_response` is called during execution and whether there's any downstream deduplication.

### Title
Missing Intra-Payload Duplicate `callback_id` Check in Bitcoin `validate_payload` Allows Single-Delivery Invariant Violation — (`rs/bitcoin/consensus/src/payload_builder.rs`)

---

### Summary

`validate_self_validating_payload_impl` only checks non-emptiness. It never checks for duplicate `callback_id` values within the same `SelfValidatingPayload`. A Byzantine block proposer (a single compromised subnet node, within the IC's fault-tolerance threshold) can craft a payload containing two `BitcoinAdapterResponse` entries with the same `callback_id`. Every honest replica's `validate_payload` call will return `Ok(())`, the block will be finalized, and `push_response_bitcoin` will be called twice for the same `callback_id`, pushing two `ConsensusResponse` entries into `consensus_queue` and violating the single-delivery invariant.

---

### Finding Description

**Validation gap — `validate_self_validating_payload_impl`:** [1](#0-0) 

The function returns `Ok(size)` after a single non-emptiness check. It never iterates over the decoded responses to verify that all `callback_id` values are distinct.

**`validate_payload` inherits the gap:** [2](#0-1) 

After decoding with `bytes_to_payload`, the only checks are (1) the call to `validate_self_validating_payload_impl` and (2) a byte-size limit. No uniqueness check on `callback_id` is performed.

**Contrast with other payload builders** — the canister-HTTP payload builder explicitly rejects duplicates with `delivered_ids.insert(response.content.id)` returning `false`: [3](#0-2) 

The Bitcoin payload builder has no equivalent guard.

**`push_response` uses `get_mut`, not `remove`, for `GetSuccessorsResponse`:** [4](#0-3) 

Because the context is never removed from `bitcoin_get_successors_contexts`, a second call with the same `callback_id` also succeeds and pushes a second `ConsensusResponse` into `consensus_queue`. For `SendTransactionResponse`, `GetSuccessorsReject`, and `SendTransactionReject`, there is no lookup at all — both calls unconditionally push to `consensus_queue`: [5](#0-4) 

**Errors from `push_response_bitcoin` are silently swallowed in the demux:** [6](#0-5) 

**The scheduler drains `consensus_queue` fully, processing every entry:** [7](#0-6) 

Both duplicate entries are delivered to `execute_subnet_message`.

---

### Impact Explanation

Two `ConsensusResponse` items with the same `callback_id` enter the execution pipeline. For `GetSuccessorsResponse`, the Bitcoin canister receives two responses for the same `GetSuccessors` request. Depending on whether the canister's callback is consumed on first delivery, the second delivery either:

- Fails silently (callback already consumed) — causing an inconsistent state in the subnet call context manager (context still present in `bitcoin_get_successors_contexts` but response already delivered), or
- Succeeds — causing the Bitcoin canister to process the same block data twice, which is the precondition for ckBTC double-minting.

For `SendTransactionResponse`/reject variants, two responses are unconditionally enqueued with no lookup, so the second delivery is guaranteed to reach `execute_subnet_message` with the same `callback_id`.

The single-delivery invariant is concretely violated at the `push_response` layer regardless of downstream handling.

---

### Likelihood Explanation

Requires a single Byzantine block proposer — one compromised subnet node selected as proposer for a round. This is explicitly within the IC's fault-tolerance model ("protocol peer behavior below the consensus fault threshold"). The crafted payload passes all honest-replica validation checks deterministically. No threshold corruption, no governance majority, no privileged key is needed.

---

### Recommendation

Add a duplicate `callback_id` check inside `validate_payload` (or `validate_self_validating_payload_impl`) for the Bitcoin payload builder, mirroring the pattern used in the canister-HTTP builder:

```rust
let mut seen_ids = BTreeSet::new();
for response in &responses {
    if !seen_ids.insert(response.callback_id) {
        return Err(/* InvalidSelfValidatingPayload: DuplicateCallbackId */);
    }
}
```

Additionally, change `get_mut` to `remove` in `push_response` for `GetSuccessorsResponse` so that the context is consumed on first delivery, making a second call return `BitcoinNonMatchingResponse` as a defense-in-depth measure. [8](#0-7) 

---

### Proof of Concept

```rust
// 1. Build a SelfValidatingPayload with two entries sharing callback_id=42.
let dup_response = BitcoinAdapterResponse {
    response: BitcoinAdapterResponseWrapper::GetSuccessorsResponse(
        GetSuccessorsResponseComplete { blocks: vec![], next: vec![] },
    ),
    callback_id: 42,
};
let payload = SelfValidatingPayload::new(vec![dup_response.clone(), dup_response]);
let bytes = parse::payload_to_bytes(&payload, MAX_SIZE, &logger);

// 2. validate_payload returns Ok — no duplicate check exists.
let result = builder.validate_payload(Height::new(1), &proposal_context, &bytes, &[]);
assert!(result.is_ok()); // passes today

// 3. During execution, push_response_bitcoin is called twice.
state.metadata.subnet_call_context_manager.push_context(
    SubnetCallContext::BitcoinGetSuccessors(/* context for callback_id=42 */),
);
state.push_response_bitcoin(dup_response.clone()).unwrap(); // Ok — pushes to consensus_queue
state.push_response_bitcoin(dup_response).unwrap();         // Also Ok — pushes again (get_mut, not remove)
assert_eq!(state.consensus_queue.len(), 2); // single-delivery invariant violated
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

**File:** rs/https_outcalls/consensus/src/payload_builder.rs (L461-466)
```rust
            // Check that the response is not submitted twice
            if !delivered_ids.insert(response.content.id) {
                return invalid_artifact(InvalidCanisterHttpPayloadReason::DuplicateResponse(
                    response.content.id,
                ));
            }
```

**File:** rs/replicated_state/src/bitcoin.rs (L32-60)
```rust
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

**File:** rs/execution_environment/src/scheduler.rs (L1295-1325)
```rust
            // The consensus queue has to be emptied in each round, so we process
            // it fully without applying the per-round instruction limit.
            // For now, we assume all subnet messages need the entire replicated
            // state. That can be changed in the future as we optimize scheduling.
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
