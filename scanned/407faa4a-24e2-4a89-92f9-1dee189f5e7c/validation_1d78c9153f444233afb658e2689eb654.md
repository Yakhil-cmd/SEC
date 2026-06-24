### Title
Canister HTTP Outcall Queue Monopolization via Absent Per-Canister Slot Fairness - (File: `rs/execution_environment/src/execution_environment.rs`)

### Summary
The subnet-wide HTTP outcall in-flight queue (`MAX_CANISTER_HTTP_REQUESTS_IN_FLIGHT = 3000`) enforces only a global cap with no per-canister allocation. A single malicious canister with sufficient cycles can fill all 3000 slots by targeting unreachable URLs, blocking every other canister on the subnet from issuing HTTP outcalls for up to `CANISTER_HTTP_TIMEOUT_INTERVAL = 60` seconds. The attacker pays the upfront legacy fee per slot but receives no refund on timeout, making this a cycles-funded sustained DoS against the subnet's HTTP outcall capacity.

### Finding Description

`try_add_http_context_to_replicated_state` in `rs/execution_environment/src/execution_environment.rs` enforces a single global gate before accepting a new HTTP outcall context:

```rust
if state
    .metadata
    .subnet_call_context_manager
    .canister_http_request_contexts
    .len()
    >= self.config.max_canister_http_requests_in_flight   // 3000
{
    return Err(UserError::new(
        ErrorCode::CanisterRejectedMessage,
        format!("max number ({}) of http requests in-flight reached.", ...),
    ));
}
``` [1](#0-0) 

There is no per-canister sub-limit, no per-canister quota, and no fairness scheduler. The constant is set to 3000 with the rationale of supporting 100 req/s at 30 s worst-case latency: [2](#0-1) 

The consensus-layer timeout that governs how long each slot is held is 60 seconds: [3](#0-2) 

Under **legacy pricing** (the default `PricingVersion`), the full legacy fee is deducted from the request payment at submission time and is **never refunded on timeout**:

```rust
PricingVersion::Legacy => {
    canister_http_request_context.request.payment -= legacy_fee.real();
}
``` [4](#0-3) 

The `LegacyTracker` explicitly documents that no cycles accounting is performed at the adapter layer, confirming no per-request refund on timeout: [5](#0-4) 

**Attack path:**

1. Deploy a canister on the target application subnet.
2. Call `ic_00::HttpRequest` 3000 times, each targeting an unreachable IPv6 address (e.g., `https://[40d:40d:40d:40d:40d:40d:40d:40d]:28992`), attaching the minimum required legacy fee per call.
3. All 3000 slots in `canister_http_request_contexts` are now occupied.
4. Every subsequent `HttpRequest` from any canister on the subnet is rejected with `"max number (3000) of http requests in-flight reached."` until the 60-second timeout expires.
5. As slots expire, the attacker submits new requests to maintain saturation (~50 req/s to hold 3000 slots at 60 s each).

The `http_request_bound_holds` test confirms the global cap is enforced and that a single caller can fill it: [6](#0-5) 

### Impact Explanation

All canisters on the targeted subnet lose the ability to make HTTP outcalls for the duration of the attack. Any application relying on HTTPS outcalls (price feeds, cross-chain bridges, oracle integrations, etc.) is completely disrupted. The impact is subnet-scoped and affects every canister equally, regardless of their own cycles balance or request history.

### Likelihood Explanation

The attack requires only a deployed canister and enough cycles to pay 3000 legacy fees. The legacy fee formula scales quadratically with subnet size but is a fixed, predictable cost. On a 13-node application subnet the fee per request is on the order of tens of billions of cycles; filling 3000 slots costs on the order of tens of trillions of cycles — a non-trivial but achievable amount for a motivated attacker. No privileged access, governance majority, or external dependency is required. The attacker-controlled entry point is the standard `ic_00::HttpRequest` management canister method, reachable by any canister on the subnet.

### Recommendation

1. **Introduce a per-canister in-flight limit** (e.g., `max_canister_http_requests_per_canister`) enforced inside `try_add_http_context_to_replicated_state`, analogous to the existing `canister_guaranteed_callback_quota` pattern used for inter-canister callbacks.
2. **Charge a non-refundable base fee on timeout** that is proportional to the time the slot was held, not just the transmission cost, to raise the economic cost of slot-squatting.
3. Consider a **per-canister sliding-window rate limit** on HTTP outcall submissions to prevent burst saturation.

### Proof of Concept

```rust
// Attacker canister pseudocode
for _ in 0..3000 {
    ic_cdk::call(
        Principal::management_canister(),
        "http_request",
        (CanisterHttpRequestArgs {
            url: "https://[40d:40d:40d:40d:40d:40d:40d:40d]:28992".to_string(),
            max_response_bytes: Some(1),   // minimize fee
            headers: vec![],
            body: None,
            method: HttpMethod::GET,
            transform: None,
            is_replicated: None,
            pricing_version: None,
        },),
    )
    .with_cycles(LEGACY_FEE)
    .await;
}
// Subnet now rejects all HttpRequest calls from any canister for ~60 seconds.
// Attacker re-floods as slots expire to maintain saturation.
```

The global check at `rs/execution_environment/src/execution_environment.rs:2148–2162` will reject every subsequent `HttpRequest` from any canister on the subnet with `CanisterRejectedMessage`, confirmed by the existing test at line 2367–2380 which shows a single caller can reach and hold the cap. [1](#0-0) [7](#0-6) [2](#0-1)

### Citations

**File:** rs/execution_environment/src/execution_environment.rs (L2148-2162)
```rust
        if state
            .metadata
            .subnet_call_context_manager
            .canister_http_request_contexts
            .len()
            >= self.config.max_canister_http_requests_in_flight
        {
            return Err(UserError::new(
                ErrorCode::CanisterRejectedMessage,
                format!(
                    "max number ({}) of http requests in-flight reached.",
                    self.config.max_canister_http_requests_in_flight
                ),
            ));
        }
```

**File:** rs/execution_environment/src/execution_environment.rs (L2207-2212)
```rust
        match canister_http_request_context.pricing_version {
            PricingVersion::Legacy => {
                // Legacy pricing deducts the full request fee from the payment.
                // The remaining payment is refunded when the response is delivered.
                canister_http_request_context.request.payment -= legacy_fee.real();
            }
```

**File:** rs/config/src/execution_environment.rs (L203-206)
```rust
/// Maximum number of http outcall requests in-flight on a subnet.
/// To support 100 req/s with a worst case request latency of 30s the queue size needs buffer 100 req/s * 30s = 3000 req.
/// The worst case request latency used here should be equivalent to the request timeout in the adapter.
pub const MAX_CANISTER_HTTP_REQUESTS_IN_FLIGHT: usize = 3000;
```

**File:** rs/types/types/src/canister_http.rs (L78-85)
```rust
/// Time after which a response is considered timed out and a timeout error will be returned to execution
pub const CANISTER_HTTP_TIMEOUT_INTERVAL: Duration = Duration::from_secs(60);

/// Number of CanisterHttpResponses to be included in a block.
///
/// Limiting the number of responses can improve performance, as otherwise validation times
/// could become too large.
pub const CANISTER_HTTP_MAX_RESPONSES_PER_BLOCK: usize = 500;
```

**File:** rs/https_outcalls/pricing/src/legacy.rs (L48-52)
```rust
    fn create_payment_receipt(&self) -> CanisterHttpPaymentReceipt {
        // Legacy pricing does not perform cycles accounting, so no cycles
        // are ever refunded.
        CanisterHttpPaymentReceipt::default()
    }
```

**File:** rs/execution_environment/src/execution_environment/tests.rs (L2320-2381)
```rust
fn http_request_bound_holds() {
    let own_subnet = subnet_test_id(1);
    let caller_canister = canister_test_id(10);
    let mut test = ExecutionTestBuilder::new()
        .with_own_subnet_id(own_subnet)
        .with_caller(own_subnet, caller_canister)
        // set number of max in-flight calls to 10
        .with_max_canister_http_requests_in_flight(10)
        .build();
    test.state_mut().metadata.own_subnet_features.http_requests = true;

    // Create payload of the request.
    let url = "https://".to_string();
    let response_size_limit = 1000_u64;
    let transform_method_name = "transform".to_string();
    let transform_context = vec![0, 1, 2];
    let args = CanisterHttpRequestArgs {
        url: url.clone(),
        max_response_bytes: Some(response_size_limit),
        headers: BoundedHttpHeaders::new(vec![]),
        body: None,
        method: HttpMethod::GET,
        transform: Some(TransformContext {
            function: TransformFunc(candid::Func {
                principal: caller_canister.get().0,
                method: transform_method_name.clone(),
            }),
            context: transform_context.clone(),
        }),
        is_replicated: None,
        pricing_version: None,
    };

    // Create request to HTTP_REQUEST method.
    let payload = args.clone().encode();
    test.inject_call_to_ic00(Method::HttpRequest, payload, Cycles::new(1_000_000_000));
    test.execute_all();
    // Check that the SubnetCallContextManager contains the request.
    let canister_http_request_contexts = &test
        .state()
        .metadata
        .subnet_call_context_manager
        .canister_http_request_contexts;
    assert_eq!(canister_http_request_contexts.len(), 1);

    // Now we try to inject more than the maximum number of requests we allow to be in-flight

    for _ in 0..15 {
        let payload = args.clone().encode();
        test.inject_call_to_ic00(Method::HttpRequest, payload, Cycles::new(1_000_000_000));
    }

    test.execute_all();
    let canister_http_request_contexts = &test
        .state()
        .metadata
        .subnet_call_context_manager
        .canister_http_request_contexts;

    // Check that the SubnetCallContextManager contains the maximum number but not more
    assert_eq!(canister_http_request_contexts.len(), 10);
}
```
