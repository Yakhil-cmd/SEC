Audit Report

## Title
`validate_self_validating_payload_impl` Accepts Fabricated Bitcoin Adapter Responses Without Callback ID Validation — (`rs/bitcoin/consensus/src/payload_builder.rs`)

## Summary

`BitcoinPayloadBuilder::validate_self_validating_payload_impl` performs no validation of `BitcoinAdapterResponse` entries beyond checking byte size. A single malicious block-proposing node can craft a `SelfValidatingPayload` containing fabricated Bitcoin adapter responses using real pending `callback_id` values. All other replicas accept the block because their validation also only checks size. The execution layer's `push_response` function then processes the fabricated `GetSuccessorsResponse` as legitimate Bitcoin network data, updating the UTXO set with attacker-controlled content, enabling illegal ckBTC minting.

## Finding Description

**Consensus validation path** (`rs/bitcoin/consensus/src/payload_builder.rs`, lines 234–251):

```rust
fn validate_self_validating_payload_impl(
    &self,
    payload: &SelfValidatingPayload,
    validation_context: &ValidationContext,
) -> Result<NumBytes, SelfValidatingPayloadValidationError> {
    let since = Instant::now();
    if *payload == SelfValidatingPayload::default() {
        return Ok(0.into());
    }
    self.metrics.observe_validate_duration(VALIDATION_STATUS_VALID, since);
    let size = NumBytes::new(payload.count_bytes() as u64);
    Ok(size)
}
``` [1](#0-0) 

The `BatchPayloadBuilder::validate_payload` implementation (lines 357–397) calls this function and additionally checks `MAX_BITCOIN_PAYLOAD_IN_BYTES`, but performs no callback ID lookup against the certified state. [2](#0-1) 

**Execution layer processing** (`rs/replicated_state/src/bitcoin.rs`, `push_response`, lines 23–103):

For `GetSuccessorsResponse`, the execution layer does validate that the `callback_id` exists in `bitcoin_get_successors_contexts`:

```rust
let context = state
    .metadata
    .subnet_call_context_manager
    .bitcoin_get_successors_contexts
    .get_mut(&callback_id)
    .ok_or_else(|| StateError::BitcoinNonMatchingResponse { callback_id: callback_id.get() })?;
``` [3](#0-2) 

This check passes when the attacker uses a **real** pending `callback_id`. The fabricated Bitcoin block content is then accepted without any proof-of-work or content verification, and the response is pushed to the consensus queue: [4](#0-3) 

For `SendTransactionResponse`, there is **no** callback ID existence check at all — the execution layer unconditionally pushes a response to the consensus queue for any `callback_id`: [5](#0-4) 

**Contrast with canister HTTP validation** (`rs/https_outcalls/consensus/src/payload_builder.rs`, lines 468–473), which correctly rejects any response whose `callback_id` is not found in `canister_http_request_contexts`:

```rust
let request_context = http_contexts.get(&callback_id).ok_or(
    CanisterHttpPayloadValidationError::InvalidArtifact(
        InvalidCanisterHttpPayloadReason::UnknownCallbackId(callback_id),
    ),
)?;
``` [6](#0-5) 

**Honest payload builder** (`rs/bitcoin/consensus/src/payload_builder.rs`, lines 126–210) iterates only over real pending requests from certified state and assigns each response the correct `callback_id`. The validation path has no equivalent check. [7](#0-6) 

## Impact Explanation

A malicious block proposer can inject a `GetSuccessorsResponse` with a real pending `callback_id` and fabricated Bitcoin block data (e.g., a fake block crediting the attacker's address with a large UTXO). The execution layer's only defense — checking that the `callback_id` exists — passes because the attacker uses a real pending ID. The fabricated blocks are delivered to the Bitcoin management canister, which updates its UTXO set. The ckBTC minter then observes the fake UTXO and mints ckBTC with no corresponding real Bitcoin deposit.

This constitutes **illegal minting of an in-scope chain-key asset (ckBTC)** and maps to the Critical impact class: *"Theft, permanent loss, illegal minting, or protocol insolvency involving exorbitant ICP/Cycles or in-scope chain-key/ledger assets."* [8](#0-7) 

## Likelihood Explanation

Every node on the Bitcoin-enabled subnet participates in block proposal rotation. A single compromised or malicious node operator — well below the consensus fault threshold — will eventually be selected as block proposer. No threshold cryptography, majority collusion, or admin key is required. The attacker only needs to: (1) read the current certified state to obtain a live `callback_id` for a pending `GetSuccessorsRequest`, and (2) craft a `SelfValidatingPayload` within `MAX_BITCOIN_PAYLOAD_IN_BYTES` containing a fabricated `GetSuccessorsResponseComplete`. This is trivially achievable by any node operator who controls the replica binary. [9](#0-8) 

## Recommendation

`validate_self_validating_payload_impl` must load the certified state at `validation_context.certified_height` (as `get_self_validating_payload_impl` already does) and, for each `BitcoinAdapterResponse` in the payload:

1. Verify `response.callback_id` exists in `subnet_call_context_manager.bitcoin_get_successors_contexts` or `bitcoin_send_transaction_internal_contexts`.
2. Verify the response variant matches the request type stored for that `callback_id` (a `GetSuccessorsResponse` must correspond to a `GetSuccessorsRequest`).
3. Reject any payload containing duplicate `callback_id` values.

This mirrors the pattern already correctly implemented in `validate_canister_http_payload_impl`. [10](#0-9) 

## Proof of Concept

1. Attacker controls one replica node on the Bitcoin-enabled subnet.
2. Attacker reads the current certified state to obtain a live `callback_id` for a pending `GetSuccessorsRequest` in `subnet_call_context_manager.bitcoin_get_successors_contexts`.
3. When the attacker's node is selected as block proposer, it constructs a `SelfValidatingPayload` containing a `BitcoinAdapterResponse { response: GetSuccessorsResponseComplete { blocks: [<fake_block>], next: [] }, callback_id: <real_id> }`.
4. The block is proposed. Every other replica calls `validate_self_validating_payload_impl`; the payload passes because its byte size is within `MAX_BITCOIN_PAYLOAD_IN_BYTES`.
5. The block is finalized. `push_response` is called; the `callback_id` lookup in `bitcoin_get_successors_contexts` succeeds (real ID), and the fabricated blocks are pushed to the consensus queue.
6. The Bitcoin management canister processes the fake block and updates its UTXO set.
7. The attacker calls `update_balance` on the ckBTC minter; the minter observes the fake UTXO and mints ckBTC to the attacker's account.

A deterministic integration test can reproduce this by: constructing a `BitcoinPayloadBuilder` with a mock state containing one pending `GetSuccessorsRequest`, calling `validate_payload` with a hand-crafted `SelfValidatingPayload` using the real `callback_id` but fabricated block content, and asserting `Ok(())` is returned — confirming the fabricated payload passes consensus validation. [11](#0-10)

### Citations

**File:** rs/bitcoin/consensus/src/payload_builder.rs (L109-121)
```rust
    fn get_self_validating_payload_impl(
        &self,
        validation_context: &ValidationContext,
        past_callback_ids: BTreeSet<u64>,
        byte_limit: NumBytes,
        priority: usize,
    ) -> Result<SelfValidatingPayload, GetPayloadError> {
        // Retrieve the `ReplicatedState` required by `validation_context`.
        let state = self
            .state_manager
            .get_state_at(validation_context.certified_height)
            .map_err(|e| GetPayloadError::GetStateFailed(validation_context.certified_height, e))?
            .take();
```

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

**File:** rs/replicated_state/src/bitcoin.rs (L23-62)
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

**File:** rs/https_outcalls/consensus/src/payload_builder.rs (L468-473)
```rust
            let callback_id = response.content.id;
            let request_context = http_contexts.get(&callback_id).ok_or(
                CanisterHttpPayloadValidationError::InvalidArtifact(
                    InvalidCanisterHttpPayloadReason::UnknownCallbackId(callback_id),
                ),
            )?;
```
