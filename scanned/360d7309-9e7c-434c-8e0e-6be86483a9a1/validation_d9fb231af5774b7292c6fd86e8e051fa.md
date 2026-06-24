### Title
Byzantine Block Proposer Can Inject Multi-Block `GetSuccessorsResponse` to Force Spurious `CanisterError` Reject on Bitcoin Canister — (`rs/replicated_state/src/bitcoin.rs`)

---

### Summary

A Byzantine block proposer can craft a `SelfValidatingPayload` containing a `GetSuccessorsResponseComplete` with `blocks.len() > 1` and `count_bytes() > MAX_RESPONSE_SIZE` (2,000,000). The payload validation does not check block count within a response, so the block is notarized and finalized. During execution, `push_response` calls `maybe_split_response`, which returns `Err(SplitError::NotOneBlock)`, causing a `Payload::Reject(RejectCode::CanisterError)` to be enqueued for the bitcoin canister's pending callback — a spurious error for a legitimately-pending request.

---

### Finding Description

**`maybe_split_response` error path:**

In `rs/replicated_state/src/bitcoin.rs`, `maybe_split_response` checks:

```rust
if response.count_bytes() > MAX_RESPONSE_SIZE {
    if response.blocks.len() != 1 {
        return Err(SplitError::NotOneBlock);
    }
    ...
}
``` [1](#0-0) 

`push_response` converts this error directly into a `Payload::Reject(CanisterError)` and pushes it to the consensus queue:

```rust
Err(err) => Payload::Reject(RejectContext::new(
    RejectCode::CanisterError,
    format!("Received invalid response from adapter: {err:?}"),
)),
``` [2](#0-1) 

**Validation gap in `BitcoinPayloadBuilder`:**

`validate_self_validating_payload_impl` performs no content validation — it only checks whether the payload is empty:

```rust
if *payload == SelfValidatingPayload::default() {
    return Ok(0.into());
}
// ...
Ok(size)
``` [3](#0-2) 

The `BatchPayloadBuilder::validate_payload` implementation checks total serialized size against `MAX_BITCOIN_PAYLOAD_IN_BYTES` (4,100,000 bytes), but when `num_responses == 1` it only **warns** even if oversized, and never inspects `blocks.len()` within a single response:

```rust
if raw_payload_len as u64 > MAX_BITCOIN_PAYLOAD_IN_BYTES {
    if num_responses == 1 {
        warn!(self.log, "Bitcoin Payload oversized");
    } else {
        return Err(...PayloadTooBig...);
    }
}
Ok(())
``` [4](#0-3) 

`num_responses` counts `BitcoinAdapterResponse` items, not blocks within a response. A single `BitcoinAdapterResponse` wrapping a `GetSuccessorsResponseComplete` with 2 blocks of ~1.5 MB each (total ~3 MB) passes all validation checks.

**The existing test confirms the code path is reachable:** [5](#0-4) 

---

### Impact Explanation

A Byzantine block proposer with a valid `callback_id` (observable from replicated state, which is visible to all subnet nodes) can:

1. Craft a `SelfValidatingPayload` with one `BitcoinAdapterResponse` containing a `GetSuccessorsResponseComplete` with 2 blocks totaling > 2 MB but < 4.1 MB.
2. The payload passes all validation checks and is notarized/finalized.
3. During deterministic execution, `push_response` enqueues `Payload::Reject(CanisterError, "Received invalid response from adapter: NotOneBlock")` for the bitcoin canister's pending callback.
4. The bitcoin canister receives a spurious error for a legitimate `GetSuccessors` request.
5. The attacker can repeat this every time they are the block proposer, continuously injecting errors and preventing the bitcoin canister from tracking the Bitcoin chain.

---

### Likelihood Explanation

- Requires controlling a single subnet node (below the fault threshold) — within the "protocol peer behavior" threat model.
- The `callback_id` is observable from replicated state by any subnet node.
- The attack is repeatable on every round the Byzantine node is the block proposer.
- The existing test `bitcoin_get_successors_pagination_invalid_adapter_request` already demonstrates the exact reject message produced. [6](#0-5) 

---

### Recommendation

Add a structural validity check in `validate_payload` (or `validate_self_validating_payload_impl`) that mirrors the preconditions of `maybe_split_response`: if a `GetSuccessorsResponseComplete` has `count_bytes() > MAX_RESPONSE_SIZE`, reject the payload unless `blocks.len() == 1`. This mirrors the invariant already enforced at execution time but currently absent at validation time. [7](#0-6) 

---

### Proof of Concept

State-machine test:

1. Register a `GetSuccessors` context with `callback_id = 0` in the replicated state.
2. Construct a `SelfValidatingPayload` containing one `BitcoinAdapterResponse` with `callback_id = 0` and a `GetSuccessorsResponseComplete { blocks: vec![vec![0u8; 1_500_000], vec![0u8; 1_500_000]], next: vec![] }` (total ~3 MB > `MAX_RESPONSE_SIZE`).
3. Call `BitcoinPayloadBuilder::validate_payload` — assert it returns `Ok(())`.
4. Deliver the payload to `push_response` via `state.push_response_bitcoin(...)`.
5. Assert `state.consensus_queue[0].payload == Payload::Reject(RejectContext { code: CanisterError, message: "Received invalid response from adapter: NotOneBlock" })`.

The existing unit test at `rs/replicated_state/src/bitcoin.rs` lines 160–176 already confirms step 4–5 in isolation: [8](#0-7)

### Citations

**File:** rs/replicated_state/src/bitcoin.rs (L51-54)
```rust
                Err(err) => Payload::Reject(RejectContext::new(
                    RejectCode::CanisterError,
                    format!("Received invalid response from adapter: {err:?}"),
                )),
```

**File:** rs/replicated_state/src/bitcoin.rs (L117-120)
```rust
    if response.count_bytes() > MAX_RESPONSE_SIZE {
        if response.blocks.len() != 1 {
            return Err(SplitError::NotOneBlock);
        }
```

**File:** rs/replicated_state/src/bitcoin.rs (L160-176)
```rust
    fn maybe_split_response_returns_error_if_not_exactly_one_block() {
        assert_eq!(
            maybe_split_response(GetSuccessorsResponseComplete {
                blocks: vec![vec![0; MAX_RESPONSE_SIZE], vec![0]], // two blocks exceeding size.
                next: vec![],
            }),
            Err(SplitError::NotOneBlock)
        );

        assert_eq!(
            maybe_split_response(GetSuccessorsResponseComplete {
                blocks: vec![],
                next: vec![vec![0; MAX_RESPONSE_SIZE + 1]],
            }),
            Err(SplitError::NotOneBlock)
        );
    }
```

**File:** rs/bitcoin/consensus/src/payload_builder.rs (L242-250)
```rust
        if *payload == SelfValidatingPayload::default() {
            return Ok(0.into());
        }

        self.metrics
            .observe_validate_duration(VALIDATION_STATUS_VALID, since);
        let size = NumBytes::new(payload.count_bytes() as u64);

        Ok(size)
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

**File:** rs/replica_tests/tests/bitcoin.rs (L276-309)
```rust
#[test]
fn bitcoin_get_successors_pagination_invalid_adapter_request() {
    bitcoin_test(
        // A mock adapter response returning a large payload that doesn't fit.
        MockBitcoinAdapterBuilder::new()
            .with_get_successors_reply(BtcServiceGetSuccessorsResponse {
                blocks: vec![vec![0; 4_000_000], vec![0]],
                next: vec![],
            })
            .build(),
        |runtime| {
            let canister_id = runtime.create_universal_canister();
            let canister = ic_replica_tests::UniversalCanister {
                runtime,
                canister_id,
            };

            let response = call_get_successors(
                &canister,
                ic00::BitcoinGetSuccessorsArgs::Initial(ic00::BitcoinGetSuccessorsRequestInitial {
                    network: ic_btc_replica_types::Network::BitcoinRegtest,
                    anchor: vec![],
                    processed_block_hashes: vec![],
                }),
            );

            assert_eq!(
                response,
                WasmResult::Reject(
                    "Received invalid response from adapter: NotOneBlock".to_string()
                )
            );
        },
    );
```
