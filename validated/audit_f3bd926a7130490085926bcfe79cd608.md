Looking at the two key files in detail:

**`validate_self_validating_payload_impl`** (the core of `validate_payload`): [1](#0-0) 

It only checks whether the payload is empty or returns its byte size — it performs **no** check that a response's type matches the pending request type for a given `callback_id`, and no check that the `callback_id` even exists in the state.

**`push_response` — `SendTransactionResponse` branch**: [2](#0-1) 

Unlike the `GetSuccessorsResponse` branch (which does a `.get_mut(&callback_id).ok_or_else(...)` lookup in `bitcoin_get_successors_contexts`), the `SendTransactionResponse` branch performs **no lookup** against `bitcoin_send_transaction_internal_contexts`. It unconditionally constructs `Payload::Data(EmptyBlob.encode())` and pushes it to the consensus queue for whatever `callback_id` was supplied.

---

### Title
Byzantine Block Proposer Can Deliver `EmptyBlob` to a Pending `GetSuccessors` Callback via Response-Type/Callback-ID Mismatch — (`rs/bitcoin/consensus/src/payload_builder.rs`, `rs/replicated_state/src/bitcoin.rs`)

### Summary

`validate_payload` does not verify that the response type in a `BitcoinAdapterResponse` matches the request type registered for the given `callback_id`. A single Byzantine block proposer (below the consensus fault threshold) can craft a payload containing `SendTransactionResponse` paired with a `GetSuccessors` callback ID. Every honest node's `validate_payload` will accept it, the block will be notarized and finalized, and `push_response` will enqueue `Payload::Data(EmptyBlob)` for the `GetSuccessors` callback — corrupting the bitcoin canister's chain-sync state.

### Finding Description

**Step 1 — Validation gap.**
`validate_payload` decodes the payload and delegates to `validate_self_validating_payload_impl`: [3](#0-2) 

`validate_self_validating_payload_impl` is effectively a no-op beyond an empty-payload short-circuit and a byte-count return: [1](#0-0) 

It never reads the replicated state to check whether `callback_id` G belongs to a `GetSuccessors` context or a `SendTransaction` context, and never verifies that the response variant matches the request variant.

**Step 2 — Unchecked `SendTransactionResponse` branch in `push_response`.**
When `push_response` receives a `SendTransactionResponse`, it skips any context lookup: [4](#0-3) 

Compare with the `GetSuccessorsResponse` branch, which guards with `.ok_or_else(|| StateError::BitcoinNonMatchingResponse {...})?`: [5](#0-4) 

No equivalent guard exists for `SendTransactionResponse`. The function returns `Ok(())` and the `EmptyBlob` response is committed to the consensus queue regardless of whether the `callback_id` belongs to a `GetSuccessors` or `SendTransaction` context.

**Step 3 — Attack path.**
A Byzantine block proposer constructs a `SelfValidatingPayload` containing:
```
BitcoinAdapterResponse {
    response: SendTransactionResponse({}),
    callback_id: G,   // G is a pending GetSuccessors callback_id
}
``` [6](#0-5) 

All honest replicas call `validate_payload`, which accepts the payload (only size and decodability are checked). The block is notarized and finalized. During execution, `push_response` is called with this response, enters the `SendTransactionResponse` arm, and enqueues `ConsensusResponse::new(G, Payload::Data(EmptyBlob.encode()))`.

### Impact Explanation

The bitcoin canister's `GetSuccessors` callback for ID G receives `EmptyBlob` instead of block data. The canister's chain-sync state machine does not expect this encoding and will either trap, misinterpret the response, or stall. The `GetSuccessors` context for G remains in `bitcoin_get_successors_contexts` (it was never removed), so the canister may be stuck waiting for a response that has already been "consumed." This directly stalls the ckBTC minter's UTXO tracking, which depends on continuous chain-sync progress.

### Likelihood Explanation

Exploitation requires control of exactly **one** subnet node acting as block proposer — a single Byzantine node below the consensus fault threshold (`f < n/3`). This is explicitly within the IC threat model and the stated scope ("protocol peer behavior below the consensus fault threshold"). No threshold corruption, key compromise, or privileged access is needed. The attack is deterministic and locally reproducible.

### Recommendation

1. **In `validate_payload`**: Read the certified state and, for each response in the payload, verify that the `callback_id` exists in the appropriate context map and that the response variant matches the request variant (e.g., `SendTransactionResponse` only for IDs in `bitcoin_send_transaction_internal_contexts`). [6](#0-5) 

2. **In `push_response` — `SendTransactionResponse` branch**: Add a lookup against `bitcoin_send_transaction_internal_contexts` (mirroring the guard in the `GetSuccessorsResponse` branch) and return `StateError::BitcoinNonMatchingResponse` if the `callback_id` is absent or belongs to the wrong context type. [2](#0-1) 

### Proof of Concept

```rust
// State-machine test sketch
let mut state = make_replicated_state();

// Register a GetSuccessors context with callback_id = 0
state.metadata.subnet_call_context_manager
    .bitcoin_get_successors_contexts
    .insert(CallbackId::from(0), make_get_successors_context());

// Byzantine proposer crafts a SendTransactionResponse for callback_id 0
let malicious_response = BitcoinAdapterResponse {
    response: BitcoinAdapterResponseWrapper::SendTransactionResponse(
        SendTransactionResponse {}
    ),
    callback_id: 0,
};

// validate_payload accepts it (no type-match check)
// push_response returns Ok(()) and enqueues EmptyBlob for callback 0
let result = push_response(&mut state, malicious_response);
assert_eq!(result, Ok(()));
assert_eq!(
    state.consensus_queue[0].payload,
    Payload::Data(EmptyBlob.encode())  // GetSuccessors callback got EmptyBlob
);
// bitcoin_get_successors_contexts[0] still present — context leaked
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
