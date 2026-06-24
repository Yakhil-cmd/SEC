### Title
Bitcoin Adapter Response Duplicate Delivery Validation Bypass — (`File: rs/bitcoin/consensus/src/payload_builder.rs`)

---

### Summary

The `BitcoinPayloadBuilder` computes past-delivered callback IDs during payload validation but never uses them to reject duplicate Bitcoin adapter responses. A malicious block proposer below the consensus fault threshold can include already-delivered Bitcoin adapter responses in a new block, and all other validators will accept it as valid, causing duplicate responses to be executed by the Bitcoin integration canisters.

---

### Finding Description

The vulnerability class from the external report is an **incomplete state-change guard**: a value is computed to detect whether a condition has already been satisfied, but the comparison step is missing, allowing repeated invocations even when the underlying data has not changed.

The IC analog exists in `rs/bitcoin/consensus/src/payload_builder.rs` across both validation interfaces implemented by `BitcoinPayloadBuilder`.

**Path 1 — `BatchPayloadBuilder::validate_payload` (lines 357–397):**

```rust
fn validate_payload(..., past_payloads: &[PastPayload]) -> Result<(), PayloadValidationError> {
    ...
    let delivered_ids = parse::parse_past_payload_ids(past_payloads, &self.log); // computed
    let payload = parse::bytes_to_payload(payload)?;
    let num_responses = payload.len();

    let _ = self.validate_self_validating_payload_impl(   // delivered_ids is NEVER passed
        &SelfValidatingPayload::new(payload),
        proposal_context.validation_context,
    )?;
    ...
}
```

`delivered_ids` is computed from past payloads but is never forwarded to `validate_self_validating_payload_impl`. [1](#0-0) 

**Path 2 — `SelfValidatingPayloadBuilder::validate_self_validating_payload` (lines 294–301):**

```rust
fn validate_self_validating_payload(
    &self,
    payload: &SelfValidatingPayload,
    validation_context: &ValidationContext,
    _past_payloads: &[&SelfValidatingPayload],  // silently ignored
) -> Result<NumBytes, SelfValidatingPayloadValidationError> {
    self.validate_self_validating_payload_impl(payload, validation_context)
}
```

`_past_payloads` is explicitly discarded (underscore prefix). [2](#0-1) 

**`validate_self_validating_payload_impl` itself (lines 234–251) performs no duplicate check at all** — it only checks whether the payload is empty and returns the byte size:

```rust
fn validate_self_validating_payload_impl(
    &self,
    payload: &SelfValidatingPayload,
    validation_context: &ValidationContext,
) -> Result<NumBytes, SelfValidatingPayloadValidationError> {
    if *payload == SelfValidatingPayload::default() {
        return Ok(0.into());
    }
    ...
    Ok(size)
}
``` [3](#0-2) 

Contrast this with `build_payload` (the payload-construction path), which **correctly** passes `delivered_ids` to `get_self_validating_payload_impl`, which then filters out already-delivered callback IDs at line 129:

```rust
if past_callback_ids.contains(&callback_id.get()) {
    continue;
}
``` [4](#0-3) 

The asymmetry is exact: building filters duplicates; validation does not.

The file also carries `#![allow(dead_code, unused_variables)]` at line 1, which suppresses the Rust compiler warning that would otherwise flag `delivered_ids` as an unused variable in `validate_payload`. [5](#0-4) 

---

### Impact Explanation

A malicious block proposer (a subnet node acting below the consensus fault threshold) can craft a `SelfValidatingPayload` that re-includes a `BitcoinAdapterResponse` whose `callback_id` was already present in a past block. Because `validate_payload` and `validate_self_validating_payload` both skip the duplicate check, every other honest validator will accept the block as valid.

The duplicate response is then delivered to the execution layer via `IntoMessages`, which unconditionally converts every entry in the payload into a `ConsensusResponse`:

```rust
let responses = messages.responses.into_iter().map(|response| {
    ConsensusResponse::new(response.content.id, ...)
});
``` [6](#0-5) 

For Bitcoin specifically, this means:
- A `GetSuccessors` response can be replayed, causing the ckBTC minter or any Bitcoin-integrated canister to process the same Bitcoin block data twice.
- A `SendTransaction` response can be replayed, causing the canister to believe a transaction was confirmed a second time.

This constitutes a **message-routing replay bug** with direct impact on chain-fusion (ckBTC) correctness.

---

### Likelihood Explanation

The attacker must be a block proposer on a Bitcoin-enabled subnet. Block proposership rotates among all subnet nodes, so any single compromised or malicious node can exploit this during its proposer turn without requiring a majority. The exploit requires no special cryptographic material — only the ability to craft a `SelfValidatingPayload` containing a previously-seen `callback_id`, which is trivially observable from the public chain history.

---

### Recommendation

Pass `delivered_ids` / `past_payloads` into the validation logic and reject any response whose `callback_id` is already present. Specifically:

1. Extend `validate_self_validating_payload_impl` to accept a `past_callback_ids: &BTreeSet<u64>` parameter and return an error if any response's `callback_id` is in the set.
2. In `BatchPayloadBuilder::validate_payload`, pass the computed `delivered_ids` to that function instead of discarding it.
3. In `SelfValidatingPayloadBuilder::validate_self_validating_payload`, derive the past callback IDs from `past_payloads` and pass them to the same function instead of ignoring `_past_payloads`.
4. Remove or scope the `#![allow(dead_code, unused_variables)]` pragma so the compiler can flag future regressions of this kind.

---

### Proof of Concept

1. Observe a Bitcoin-enabled subnet block at height `H` containing a `SelfValidatingPayload` with a `BitcoinAdapterResponse` for `callback_id = 42`.
2. As a block proposer at height `H+N`, craft a new `SelfValidatingPayload` that again includes a `BitcoinAdapterResponse` with `callback_id = 42`.
3. Submit the block. Every validator calls `validate_payload` (or `validate_self_validating_payload`). Both paths skip the duplicate check — `delivered_ids` is computed but unused; `_past_payloads` is discarded. Validation returns `Ok(())`.
4. The block is finalized. The execution layer receives two `ConsensusResponse` messages for `callback_id = 42`, causing the Bitcoin-integrated canister to process the same response twice.

### Citations

**File:** rs/bitcoin/consensus/src/payload_builder.rs (L1-1)
```rust
#![allow(dead_code, unused_variables)]
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

**File:** rs/bitcoin/consensus/src/payload_builder.rs (L294-301)
```rust
    fn validate_self_validating_payload(
        &self,
        payload: &SelfValidatingPayload,
        validation_context: &ValidationContext,
        _past_payloads: &[&SelfValidatingPayload],
    ) -> Result<NumBytes, SelfValidatingPayloadValidationError> {
        self.validate_self_validating_payload_impl(payload, validation_context)
    }
```

**File:** rs/bitcoin/consensus/src/payload_builder.rs (L369-382)
```rust
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
```

**File:** rs/https_outcalls/consensus/src/payload_builder.rs (L927-941)
```rust
        let responses = messages.responses.into_iter().map(|response| {
            if response.proof.signatures.len() == 1 {
                stats.single_signature_responses += 1;
            }
            stats.responses += 1;
            ConsensusResponse::new(
                response.content.id,
                match response.content.content {
                    CanisterHttpResponseContent::Success(data) => Payload::Data(data),
                    CanisterHttpResponseContent::Reject(canister_http_reject) => {
                        Payload::Reject(RejectContext::from(&canister_http_reject))
                    }
                },
            )
        });
```
