Audit Report

## Title
Subnet-Wide HTTP Outcall Slot Pool Exhaustion by a Single Canister — (File: `rs/execution_environment/src/execution_environment.rs`)

## Summary
The function `try_add_http_context_to_replicated_state` enforces only a single aggregate cap of 3,000 in-flight HTTP outcall contexts across the entire subnet with no per-canister sub-limit. A single canister can therefore occupy all 3,000 slots, causing every other canister on the subnet to receive `CanisterRejectedMessage` errors for HTTP outcalls for the full duration of the attack. On subnets with a free cost schedule the attack costs zero cycles.

## Finding Description
`MAX_CANISTER_HTTP_REQUESTS_IN_FLIGHT` is defined as `3000` in `rs/config/src/execution_environment.rs` (L203–206). The sole enforcement point is in `try_add_http_context_to_replicated_state` (L2115) in `rs/execution_environment/src/execution_environment.rs`, which checks:

```rust
if state
    .metadata
    .subnet_call_context_manager
    .canister_http_request_contexts
    .len()
    >= self.config.max_canister_http_requests_in_flight
```

(L2148–2162). This counts the total across **all** canisters on the subnet. There is no secondary check on how many of those slots belong to `request.sender`. A grep search across `rs/execution_environment/src/` confirms no per-canister sub-limit exists anywhere in the path.

The payment guard at L2172 is trivially satisfied on subnets with `CanisterCyclesCostSchedule::Free` because `CyclesUseCase::HTTPOutcalls` paired with `Free` returns `Cycles::zero()` (L107–110 of `rs/types/cycles/src/compound_cycles.rs`), making `charged_fee.real()` equal to zero.

The 60-second drain window is defined by `CANISTER_HTTP_TIMEOUT_INTERVAL` (L78–79 of `rs/types/types/src/canister_http.rs`). An attacker can re-issue requests before slots expire, sustaining the denial indefinitely.

The existing test `http_request_bound_holds` (L2319–2381 of `rs/execution_environment/src/execution_environment/tests.rs`) already demonstrates that a single canister can fill the pool to the configured maximum, confirming the absence of any per-canister fairness control.

## Impact Explanation
All canisters on the targeted subnet that attempt `ic00::HttpRequest` while the pool is saturated receive `CanisterRejectedMessage` with the message `"max number (3000) of http requests in-flight reached."` This is a complete, sustained denial of the HTTP outcalls feature for all co-tenants of the subnet. This matches the allowed impact: **High ($2,000–$10,000) — Application/platform-level DoS or subnet availability impact not based on raw volumetric DDoS.**

## Likelihood Explanation
The attacker only needs to deploy a canister on a subnet with HTTP outcalls enabled — an unprivileged action available to any developer. No special keys, governance majority, or threshold corruption is required. The attack is mechanically trivial: call `ic00::HttpRequest` in a loop from an update method. The 3,000-slot pool can be saturated within a single consensus round. On free-cost-schedule subnets the attack costs nothing; on normal subnets a well-funded canister can sustain it economically. Likelihood is high.

## Recommendation
Introduce a per-canister sub-limit before inserting a new context into `canister_http_request_contexts`. In `try_add_http_context_to_replicated_state`, after the global count check, count how many existing entries have `context.request.sender == request.sender` and reject if that count exceeds a per-canister cap (e.g., `MAX_CANISTER_HTTP_REQUESTS_IN_FLIGHT / expected_active_canisters`, or a fixed value such as 500). This mirrors the fairness model already applied to ingress message selection via the round-robin per-canister quota in `rs/ingress_manager/src/ingress_selector.rs` (L159–165).

## Proof of Concept
1. Deploy canister `attacker` on any application subnet with HTTP outcalls enabled.
2. In an update method, call `ic00::HttpRequest` in a loop 3,000 times, each targeting a slow or non-existent URL with `max_response_bytes = Some(1)` to keep slots occupied for the full 60-second timeout.
3. Observe that `state.metadata.subnet_call_context_manager.canister_http_request_contexts.len()` reaches 3,000.
4. From any other canister on the same subnet, call `ic00::HttpRequest`. The call is immediately rejected with `"max number (3000) of http requests in-flight reached."` — confirmed by the existing test `http_request_bound_holds` at L2319–2381 of `rs/execution_environment/src/execution_environment/tests.rs`, which already demonstrates a single canister filling the pool to the configured maximum.
5. Re-issue step 2 before the 60-second timeout expires to sustain the denial indefinitely. On a free-cost-schedule subnet, step 2 requires zero cycles.