Audit Report

## Title
Unprivileged Canister Can Monopolize the Global Threshold-Signing Queue, Denying Service to All Other Canisters - (File: `rs/execution_environment/src/execution_environment.rs`)

## Summary
The threshold-signing request queue is enforced with a single global count per key ID, with no per-sender canister breakdown or fairness mechanism. Any canister that can pay the signing fee can fill the entire queue for a given key, causing all subsequent `sign_with_ecdsa`, `sign_with_schnorr`, or `vetkd_derive_key` calls from any other canister on the subnet to be rejected with a "queue is full" error. The attacker receives valid signatures in return and can immediately resubmit to sustain the denial of service indefinitely when no `signature_request_timeout_ns` is configured.

## Finding Description
In `rs/execution_environment/src/execution_environment.rs` at L3844–3858, the `sign_with_threshold` function checks only the global count of pending contexts for the requested key ID before accepting a new request:

```rust
if state
    .metadata
    .subnet_call_context_manager
    .sign_with_threshold_contexts_count(&threshold_key)
    >= dynamic_queue_size
{ ... reject ... }
```

`sign_with_threshold_contexts_count` (L436–453 of `rs/replicated_state/src/metadata_state/subnet_call_context_manager.rs`) iterates over all entries in the flat `BTreeMap<CallbackId, SignWithThresholdContext>` and counts those matching the key ID, with no per-sender breakdown. The `SubnetCallContextManager` struct (L212–230 of the same file) stores all signing contexts in a single flat map with no per-canister partitioning.

The effective queue capacity is computed by `get_dynamic_signature_queue_size` (L5060–5078 of `execution_environment.rs`), which is clamped to `MAX_PAIRED_PRE_SIGNATURES = 100` (L100–102 of `rs/limits/src/lib.rs`), or the registry-configured `max_queue_size` (default 20 in tests) when no pre-signatures are available.

`signature_request_timeout_ns` is `Option<u64>` (L144–147 of `rs/registry/admin/bin/create_subnet.rs`), explicitly documented as "if none is specified, no request will time out." When unset, the attacker's requests persist in the queue until consensus produces signatures, after which the attacker can immediately resubmit to keep the queue saturated.

The existing test `test_sign_with_threshold_key_queue_fills_up` (L849–905 of `rs/execution_environment/tests/threshold_signatures.rs`) directly confirms that a single canister can fill the queue to capacity and that the very next request from any canister is rejected with the "queue is full" error.

## Impact Explanation
This is a platform-level DoS on threshold signing service availability for a targeted key on a subnet. Any application or canister relying on `sign_with_ecdsa`, `sign_with_schnorr`, or `vetkd_derive_key` for that key is completely denied service for as long as the attacker sustains the attack. This matches the allowed High impact: "Application/platform-level DoS, crash, consensus blocking, certified-state disruption, or subnet availability impact not based on raw volumetric DDoS." Threshold signing is a critical service underpinning ckBTC, ckETH, ckERC20, and other Chain Fusion integrations; sustained denial of this service causes concrete, measurable harm to users and protocols depending on it.

## Likelihood Explanation
- **Entry path**: Any canister on the subnet can call `sign_with_ecdsa`/`sign_with_schnorr`/`vetkd_derive_key` — no privileged role required.
- **Cost**: The attacker pays the signing fee per request and receives valid signatures in return, making the net cost only the fee overhead (not a pure loss).
- **Queue size**: At most 100 slots (with a full pre-signature stash) or the registry-configured `max_queue_size` (default 20). Both are trivially fillable in a single batch of ingress messages.
- **Persistence**: Without `signature_request_timeout_ns`, the DoS is sustained indefinitely by continuous refilling as consensus processes each batch.
- **Detectability**: The attacker's canister ID is recorded in each `SignWithThresholdContext.request.sender`, but there is no on-chain enforcement to evict or rate-limit a single sender.

## Recommendation
1. **Per-canister quota**: Track signing-request counts per sender canister in `SubnetCallContextManager` and enforce a per-canister cap (e.g., `max_queue_size / N` where N is a configured fairness divisor), preventing any single canister from monopolizing the global queue.
2. **Mandatory timeout**: Make `signature_request_timeout_ns` required (non-optional) so that stale requests are automatically purged, bounding the duration of any DoS.
3. **Eviction of oldest requests**: When the queue is full, consider evicting the oldest unmatched request (with a reject response to its sender) to allow newer requests from other canisters to enter.

## Proof of Concept
The existing test `test_sign_with_threshold_key_queue_fills_up` in `rs/execution_environment/tests/threshold_signatures.rs` (L849–905) already demonstrates steps 2–3 of the attack from a single canister. To demonstrate the full cross-canister DoS:

1. Deploy a malicious canister `M` on a subnet with key `ecdsa:Secp256k1:key_1`, `max_queue_size = 20`, and no `signature_request_timeout_ns`.
2. `M` calls `sign_with_ecdsa` 20 times in rapid succession with sufficient cycles. All 20 are accepted and enqueued.
3. A victim canister `V` calls `sign_with_ecdsa` — it receives `"SignWithECDSA request failed: request queue for key ecdsa:Secp256k1:key_1 is full."` immediately.
4. As consensus processes `M`'s requests and returns signatures, `M` immediately resubmits, keeping the queue at capacity.
5. `V` is denied service indefinitely.

This can be reproduced as a deterministic integration test using `StateMachineBuilder` (as in the existing test), extended to use two distinct canister IDs and asserting that the second canister's request is rejected while the first canister's requests are all accepted.