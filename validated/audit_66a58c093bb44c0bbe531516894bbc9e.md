### Title
Build/Validate Asymmetry: `FlexibleCanisterHttpError::Timeout` Entries Bypass `responses_included` Counter During Build but Are Counted by `num_non_timeout_responses()` During Validation, Causing Proposer Block Rejection — (`rs/types/types/src/batch/canister_http.rs`, `rs/https_outcalls/consensus/src/payload_builder.rs`)

---

### Summary

A confirmed build/validate asymmetry exists in the Flexible HTTP outcall payload pipeline. During payload building, `FlexibleCanisterHttpError::Timeout` entries are pushed into `flexible_errors` without incrementing `responses_included`, intentionally bypassing the `CANISTER_HTTP_MAX_RESPONSES_PER_BLOCK` (500) limit. However, during validation, `num_non_timeout_responses()` counts all entries in `flexible_errors` unconditionally — including `Timeout` variants — against that same limit. An unprivileged canister developer who submits more than 500 flexible HTTP requests that all expire simultaneously will cause every block proposer to build a payload that fails its own validation, stalling subnet progress.

---

### Finding Description

**Build path** — `get_canister_http_payload_impl` in `rs/https_outcalls/consensus/src/payload_builder.rs`:

The code explicitly comments that timeouts are "not counted as responses" and are "irrelevant for the `CANISTER_HTTP_MAX_RESPONSES_PER_BLOCK` limit." For flexible requests, a `FlexibleCanisterHttpError::Timeout` is pushed to `flexible_errors` and the loop `continue`s — skipping the `responses_included` increment entirely: [1](#0-0) 

For non-flexible requests, the same bypass applies via the regular `timeouts` vec: [2](#0-1) 

The only cap on how many flexible timeouts can be included is the 2 MiB `max_payload_size`. Since each `FlexibleCanisterHttpError::Timeout` is just a `CallbackId` (8 bytes), 501 entries occupy ~4 KB — well within the limit.

**Validation path** — `validate_canister_http_payload_impl`: [3](#0-2) 

This check calls `num_non_timeout_responses()`: [4](#0-3) 

The function correctly excludes the `timeouts` field (`timeouts: _`) but **unconditionally adds `flexible_errors.len()`**, which includes `FlexibleCanisterHttpError::Timeout` variants. There is no match/filter to exclude the `Timeout` arm.

**The invariant that breaks:** The build path treats flexible timeouts as free (not counted toward the 500-response limit), but the validation path counts them against that same limit. A payload built legitimately by the proposer will fail its own validation.

---

### Impact Explanation

When >500 flexible HTTP requests time out simultaneously:

1. Every block proposer calls `get_canister_http_payload_impl`, which includes all 501+ `FlexibleCanisterHttpError::Timeout` entries in `flexible_errors` (no `responses_included` guard).
2. The proposer serializes and then validates the payload via `validate_canister_http_payload_impl`.
3. `num_non_timeout_responses()` returns 501+ → `TooManyResponses` → the block is rejected.
4. Because the timed-out request contexts remain in state until a block delivers them, and no block can be finalized while this condition persists, the subnet stalls for every round until the condition is resolved externally.

This is a **persistent, non-volumetric subnet availability impact** triggered by a single canister submitting a batch of flexible HTTP requests.

---

### Likelihood Explanation

- Requires the Flexible HTTP feature to be enabled on the target subnet.
- Requires an attacker canister to submit >500 flexible HTTP requests and wait for `CANISTER_HTTP_TIMEOUT_INTERVAL` to elapse with no responses.
- No privileged access, no key material, no majority corruption needed.
- The attacker pays cycles for the requests but the cost is bounded and one-time.

---

### Recommendation

Fix `num_non_timeout_responses()` to exclude `FlexibleCanisterHttpError::Timeout` variants from the count, mirroring how the regular `timeouts` field is already excluded:

```rust
pub fn num_non_timeout_responses(&self) -> usize {
    responses.len()
        + divergence_responses.len()
        + flexible_responses.len()
        + flexible_errors.iter().filter(|e| !matches!(e, FlexibleCanisterHttpError::Timeout { .. })).count()
}
```

This restores build/validate symmetry: flexible timeouts are free in both paths. [5](#0-4) 

---

### Proof of Concept

```
1. Enable Flexible HTTP on a test subnet.
2. Inject 501 CanisterHttpRequestContext entries with Replication::Flexible,
   all with time = UNIX_EPOCH (so they are past CANISTER_HTTP_TIMEOUT_INTERVAL).
3. Call build_payload with a validation_context.time past the timeout interval.
4. Parse the resulting bytes → assert flexible_errors.len() == 501,
   assert flexible_errors all have variant Timeout.
5. Call validate_payload on the same bytes.
6. Assert: validation returns Err(TooManyResponses { received: 501 }).
   (Expected: Ok(()))
```

The existing test `timeouts_bypass_max_responses_per_block` at [6](#0-5) 

covers the non-flexible (`timeouts` vec) case and passes. An analogous test using `Replication::Flexible` contexts would reproduce the failure.

### Citations

**File:** rs/https_outcalls/consensus/src/payload_builder.rs (L234-256)
```rust
                if request.time + CANISTER_HTTP_TIMEOUT_INTERVAL < validation_context.time {
                    // Because timeouts are very cheap to verify, they are
                    // not counted as responses (so that they are irrelevant
                    // for the CANISTER_HTTP_MAX_RESPONSES_PER_BLOCK limit.
                    if matches!(request.replication, Replication::Flexible { .. }) {
                        let error = FlexibleCanisterHttpError::Timeout {
                            callback_id: *callback_id,
                        };
                        let candidate_size = error.count_bytes();
                        let size = NumBytes::new((accumulated_size + candidate_size) as u64);
                        if size < max_payload_size {
                            flexible_errors.push(error);
                            accumulated_size += candidate_size;
                        }
                    } else {
                        let candidate_size = callback_id.count_bytes();
                        let size = NumBytes::new((accumulated_size + candidate_size) as u64);
                        if size < max_payload_size {
                            timeouts.push(*callback_id);
                            accumulated_size += candidate_size;
                        }
                    }
                    continue;
```

**File:** rs/https_outcalls/consensus/src/payload_builder.rs (L379-385)
```rust
        // Check number of responses
        if payload.num_non_timeout_responses() > CANISTER_HTTP_MAX_RESPONSES_PER_BLOCK {
            return invalid_artifact(InvalidCanisterHttpPayloadReason::TooManyResponses {
                expected: CANISTER_HTTP_MAX_RESPONSES_PER_BLOCK,
                received: payload.num_non_timeout_responses(),
            });
        }
```

**File:** rs/types/types/src/batch/canister_http.rs (L165-178)
```rust
    /// Returns the number of non_timeout responses
    pub fn num_non_timeout_responses(&self) -> usize {
        let CanisterHttpPayload {
            responses,
            timeouts: _,
            divergence_responses,
            flexible_responses,
            flexible_errors,
        } = self;
        responses.len()
            + divergence_responses.len()
            + flexible_responses.len()
            + flexible_errors.len()
    }
```

**File:** rs/https_outcalls/consensus/src/payload_builder/tests.rs (L383-431)
```rust
/// Timeouts must not count against CANISTER_HTTP_MAX_RESPONSES_PER_BLOCK.
/// Create MAX + 50 timed-out request contexts. The builder should include
/// all of them, and the resulting payload must pass validation.
#[test]
fn timeouts_bypass_max_responses_per_block() {
    let subnet_size = 4;
    let num_contexts = CANISTER_HTTP_MAX_RESPONSES_PER_BLOCK + 50;

    test_config_with_http_feature(
        true,
        subnet_size,
        |mut payload_builder, _canister_http_pool| {
            let callback_ids = 0..num_contexts as u64;

            let contexts = fully_replicated_contexts(callback_ids.clone());
            inject_request_contexts(&mut payload_builder, contexts);

            // The contexts created above use the default time = UNIX_EPOCH, so any
            // validation time beyond UNIX_EPOCH + CANISTER_HTTP_TIMEOUT_INTERVAL
            // makes those contexts time out.
            let validation_context = ValidationContext {
                registry_version: RegistryVersion::new(1),
                certified_height: Height::new(0),
                time: UNIX_EPOCH + CANISTER_HTTP_TIMEOUT_INTERVAL + Duration::from_secs(1),
            };

            let payload = payload_builder.build_payload(
                Height::new(1),
                TEST_MAX_PAYLOAD_BYTES,
                &[],
                &validation_context,
            );

            let parsed = bytes_to_payload(&payload).expect("Failed to parse payload");

            assert_eq!(parsed.num_non_timeout_responses(), 0);
            assert_eq!(parsed.timeouts.len(), num_contexts);

            payload_builder
                .validate_payload(
                    Height::new(1),
                    &test_proposal_context(&validation_context),
                    &payload,
                    &[],
                )
                .unwrap();
        },
    );
}
```
