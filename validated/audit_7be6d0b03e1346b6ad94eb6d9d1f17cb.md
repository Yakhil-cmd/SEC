Audit Report

## Title
Single Canister Can Exhaust Subnet-Wide HTTP Outcall In-Flight Pool, Blocking All Other Canisters - (File: `rs/execution_environment/src/execution_environment.rs`)

## Summary
The function `try_add_http_context_to_replicated_state` enforces a single global cap of 3000 concurrent HTTP outcall requests across the entire subnet with no per-canister sub-limit. A single canister can fill all 3000 slots with requests to non-responsive URLs, blocking every other canister on the subnet from making HTTP outcalls for the full 60-second timeout window. The attack is repeatable indefinitely.

## Finding Description
The check at `rs/execution_environment/src/execution_environment.rs` L2148–2162 compares the total length of `canister_http_request_contexts` against `max_canister_http_requests_in_flight` (3000):

```rust
if state
    .metadata
    .subnet_call_context_manager
    .canister_http_request_contexts
    .len()
    >= self.config.max_canister_http_requests_in_flight
```

The `canister_http_request_contexts` field in `SubnetCallContextManager` is a flat `BTreeMap<CallbackId, CanisterHttpRequestContext>` with no partitioning by sender canister. There is no per-canister count, quota, or fairness mechanism anywhere in this code path. A single canister can insert up to 3000 entries before the global cap triggers. Once full, every subsequent `HttpRequest` from any canister on the subnet is rejected with `ErrorCode::CanisterRejectedMessage`. Requests remain in the pool until a consensus response arrives or `CANISTER_HTTP_TIMEOUT_INTERVAL` (60 seconds) elapses. The existing test `http_request_bound_holds` explicitly demonstrates that a single caller can fill the entire pool.

## Impact Explanation
This is a **High** severity application/platform-level DoS. The HTTP outcall feature is completely unavailable to all canisters on the affected subnet for up to 60 seconds per attack cycle. The attack is renewable: after the 60-second timeout clears the pool, the attacker immediately repeats. Production canisters relying on HTTP outcalls (e.g., oracle integrations, ckETH/ckBTC minter, EVM RPC) are fully blocked for the duration. This matches the allowed impact: *"Application/platform-level DoS, crash, consensus blocking, certified-state disruption, or subnet availability impact not based on raw volumetric DDoS."*

## Likelihood Explanation
The attack requires only a deployed canister with sufficient cycles to pay the HTTP request fee for 3000 calls per 60-second window. On subnets where `CanisterCyclesCostSchedule::Free` applies the cost is zero. On normal-cost subnets the fee is finite and predictable (quadratic in subnet size but bounded). No privileged access, governance majority, or threshold corruption is required. The attack is fully deterministic and repeatable by any unprivileged canister operator.

## Recommendation
1. Enforce a per-canister sub-limit inside `try_add_http_context_to_replicated_state` by counting entries in `canister_http_request_contexts` whose `request.sender` matches the current request sender, and rejecting if that count exceeds a per-canister cap.
2. Alternatively, maintain a separate `BTreeMap<CanisterId, usize>` tracking per-canister in-flight counts, updated on insert and removal, to avoid an O(n) scan on every request.
3. Increase the economic cost of holding slots by charging cycles proportional to time-in-flight rather than only at submission.

## Proof of Concept
The existing unit test `http_request_bound_holds` in `rs/execution_environment/src/execution_environment/tests.rs` L2319–2381 already proves a single caller can fill the entire pool. A multi-canister extension of this test would confirm the DoS:

```
1. Deploy canister A on a subnet with http_requests enabled.
2. Canister A calls ic00::HttpRequest 3000 times targeting a non-routable URL.
3. Assert: canister_http_request_contexts.len() == 3000.
4. Canister B (different principal) calls ic00::HttpRequest.
5. Assert: B receives CanisterRejectedMessage:
   "max number (3000) of http requests in-flight reached."
6. After 60 seconds, A's requests time out and are cleared.
7. A repeats step 2 — DoS is sustained indefinitely.
```