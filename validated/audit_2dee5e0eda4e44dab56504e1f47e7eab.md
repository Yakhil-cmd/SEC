### Title
Cycles Sent to Inter-Canister Query Calls Are Not Refunded to Caller — (`rs/execution_environment/tests/hypervisor.rs`)

### Summary
When a canister attaches cycles to an inter-canister call targeting a `query` method, those cycles are silently lost rather than refunded to the caller. This is an acknowledged, unfixed bug (tracked as `RUN-175`) that violates the IC protocol's cycles conservation guarantee and directly mirrors the zkSync `fallback` missing `payable` pattern: a callable entry point that silently fails to handle attached value.

### Finding Description
The IC protocol guarantees that any cycles attached to an inter-canister call that are not explicitly accepted by the callee via `ic0.msg_cycles_accept` must be returned to the caller in the response. This invariant holds for `update` methods but is broken for `query` methods called via inter-canister calls.

The bug is explicitly acknowledged in the production test suite. The test `cycles_are_refunded_if_callee_is_a_query` is marked `#[ignore]` with the comment:

```
// TODO(RUN-175): Enable the test after the bug is fixed.
``` [1](#0-0) 

The test documents the exact failure mode: canister A calls canister B's `query` method with cycles attached; canister B replies without accepting cycles; canister A's balance is **not** restored by the expected refund. The cycles are permanently lost.

The structural root cause is in the execution environment's handling of query methods called from other canisters. When the execution environment receives an inter-canister request targeting a `query`-exported method, it dispatches it as `CanisterCallOrTask::Query` in `ExecutionMode::Replicated`: [2](#0-1) 

While `ApiType::ReplicatedQuery` is listed as a valid context for `ic0.msg_cycles_accept` in the system API: [3](#0-2) 

the cycles attached to the call context are not properly tracked or returned when the query method completes without accepting them. The `CallContextAction::Reply { refund }` path that normally carries unaccepted cycles back to the caller does not function correctly for this execution path.

By contrast, the analogous test for `update` methods passes correctly: [4](#0-3) 

And the response-level refund field is correctly set to `transferred_cycles` in the manual-execution variant of the query test: [5](#0-4) 

This confirms the bug is in the final balance-crediting step, not in the response message generation.

### Impact Explanation
Cycles represent real economic value on the IC (minted from ICP). Any canister that attaches cycles to a call targeting a `query` method permanently loses those cycles — they are neither accepted by the callee nor refunded to the caller. This breaks the fundamental cycles conservation invariant of the IC protocol. Protocols that use cycles-with-call patterns (e.g., payment flows, cycle-forwarding proxies, or any canister that does not know in advance whether the target method is `query` or `update`) will silently lose funds with no error signal.

### Likelihood Explanation
The entry path requires only an unprivileged canister making a standard inter-canister call with cycles to any `query`-exported method. No special permissions, governance majority, or privileged access is required. The scenario is realistic: canister developers may not know whether a callee exports a method as `query` or `update`, and the IC protocol explicitly permits attaching cycles to any inter-canister call. The bug is silent — the call succeeds with a valid reply, but the cycles are gone.

### Recommendation
Fix the execution environment's handling of `CanisterCallOrTask::Query` responses so that unaccepted cycles from the call context are correctly included in the `refund` field of the response and credited back to the caller's balance. Re-enable the `cycles_are_refunded_if_callee_is_a_query` test to prevent regression.

### Proof of Concept
The existing ignored test in the production codebase is the proof of concept. Removing the `#[ignore]` attribute from `cycles_are_refunded_if_callee_is_a_query` and running it will reproduce the bug:

```
// TODO(RUN-175): Enable the test after the bug is fixed.
#[test]
#[ignore]
fn cycles_are_refunded_if_callee_is_a_query() {
    // Canister A calls canister B's "query" method with cycles.
    // Canister B replies without accepting cycles.
    // Expected: Canister A's balance is restored (cycles refunded).
    // Actual:   Canister A's balance is NOT restored (cycles lost).
    ...
}
``` [1](#0-0)

### Citations

**File:** rs/execution_environment/tests/hypervisor.rs (L5841-5923)
```rust
#[test]
fn cycles_are_refunded_if_not_accepted() {
    let mut test = ExecutionTestBuilder::new().build();
    let initial_cycles = Cycles::new(1_000_000_000_000);

    // Create three canisters A, B, C.
    let a_id = test.universal_canister_with_cycles(initial_cycles).unwrap();
    let b_id = test.universal_canister_with_cycles(initial_cycles).unwrap();
    let c_id = test.universal_canister_with_cycles(initial_cycles).unwrap();

    let a_to_b_transferred = Cycles::from(initial_cycles.get() / 2);
    let a_to_b_accepted = Cycles::from(a_to_b_transferred.get() / 2);
    let b_to_c_transferred = a_to_b_accepted;
    let b_to_c_accepted = Cycles::from(b_to_c_transferred.get() / 2);

    // Canister C accepts some cycles and replies to canister B.
    let c = wasm()
        .accept_cycles(b_to_c_accepted)
        .message_payload()
        .append_and_reply()
        .build();

    // Canister B:
    // 1. Accepts some cycles.
    // 2. Replies to canister A.
    // 3. Calls canister C.
    // 4. Forwards the reply in the reply callback, which is the default
    //    behaviour of the universal canister.
    let b = wasm()
        .accept_cycles(a_to_b_accepted)
        .message_payload()
        .append_and_reply()
        .call_with_cycles(
            c_id,
            "update",
            call_args().other_side(c.clone()),
            b_to_c_transferred,
        )
        .build();

    // Canister A:
    // 1. Calls canister B and transfers some cycles to it.
    // 2. Forwards the reply in the reply callback, which is the default
    //    behaviour of the universal canister.
    let a = wasm()
        .call_with_cycles(
            b_id,
            "update",
            call_args().other_side(b.clone()),
            a_to_b_transferred,
        )
        .build();
    let result = test.ingress(a_id, "update", a).unwrap();
    assert_matches!(result, WasmResult::Reply(_));

    // Canister A gets a refund for all cycles not accepted by B.
    assert_eq!(
        test.canister_state(a_id).system_state.balance(),
        initial_cycles
            - test.canister_execution_cost(a_id).real()
            - test.call_fee("update", &b).real()
            - test.reply_fee(&b).real()
            - a_to_b_accepted,
    );

    // Canister B gets all cycles it accepted and a refund for all cycles not
    // accepted by C.
    assert_eq!(
        test.canister_state(b_id).system_state.balance(),
        initial_cycles
            - test.canister_execution_cost(b_id).real()
            - test.call_fee("update", &c).real()
            - test.reply_fee(&c).real()
            + a_to_b_accepted
            - b_to_c_accepted
    );

    // Canister C get all cycles it accepted.
    assert_eq!(
        test.canister_state(c_id).system_state.balance(),
        initial_cycles - test.canister_execution_cost(c_id).real() + b_to_c_accepted
    );
}
```

**File:** rs/execution_environment/tests/hypervisor.rs (L6036-6082)
```rust
// TODO(RUN-175): Enable the test after the bug is fixed.
#[test]
#[ignore]
fn cycles_are_refunded_if_callee_is_a_query() {
    let mut test = ExecutionTestBuilder::new().build();
    let initial_cycles = Cycles::new(1_000_000_000_000);

    // Create two canisters: A and B.
    let a_id = test.universal_canister_with_cycles(initial_cycles).unwrap();
    let b_id = test.universal_canister_with_cycles(initial_cycles).unwrap();

    let a_to_b_transferred = (initial_cycles.get() / 2) as u64;

    // Canister B simply replies to canister A without accepting any cycles.
    // Note that it cannot accept cycles because it runs as a query.
    let b = wasm().message_payload().append_and_reply().build();

    // Canister A:
    // 1. Calls a query method of canister B and transfers some cycles to it.
    // 2. Forwards the reply in the reply callback, which is the default
    //    behaviour of the universal canister.
    let a = wasm()
        .call_with_cycles(
            b_id,
            "query",
            call_args().other_side(b.clone()),
            Cycles::from(a_to_b_transferred),
        )
        .build();
    let result = test.ingress(a_id, "update", a).unwrap();
    assert_matches!(result, WasmResult::Reply(_));

    // Canister should get a refund for all transferred cycles.
    assert_eq!(
        test.canister_state(a_id).system_state.balance(),
        initial_cycles
            - test.canister_execution_cost(a_id).real()
            - test.call_fee("query", &b).real()
            - test.reply_fee(&b).real()
    );

    // Canister B doesn't get any transferred cycles.
    assert_eq!(
        test.canister_state(b_id).system_state.balance(),
        initial_cycles - test.canister_execution_cost(b_id).real()
    );
}
```

**File:** rs/execution_environment/src/execution_environment.rs (L2391-2428)
```rust
        match &method {
            WasmMethod::Query(_) | WasmMethod::CompositeQuery(_) => {
                let instruction_limits = InstructionLimits::new(
                    max_instructions_per_query_message,
                    instruction_limits.slice(),
                );
                let execution_parameters = self.execution_parameters(
                    &canister,
                    instruction_limits,
                    ExecutionMode::Replicated,
                    // Effectively disable subnet memory resource reservation for queries.
                    ResourceSaturation::default(),
                );
                let result = execute_call_or_task(
                    canister,
                    CanisterCallOrTask::Query(req),
                    method,
                    prepaid_execution_cycles,
                    execution_parameters,
                    time,
                    round,
                    round_limits,
                    subnet_cycles_config,
                    &self.call_tree_metrics,
                    self.config.dirty_page_logging,
                    self.deallocator_thread.sender(),
                );
                if let ExecuteMessageResult::Finished {
                    canister: _,
                    response: ExecutionResponse::Request(_),
                    instructions_used: _,
                    heap_delta: _,
                    call_duration: Some(duration),
                } = &result
                {
                    self.metrics.call_durations.observe(duration.as_secs_f64());
                }
                result
```

**File:** rs/embedders/src/wasmtime_embedder/system_api.rs (L1661-1666)
```rust
            ApiType::Update { .. }
            | ApiType::ReplicatedQuery { .. }
            | ApiType::ReplyCallback { .. }
            | ApiType::RejectCallback { .. } => {
                Ok(self.sandbox_safe_system_state.msg_cycles_accept(max_amount))
            }
```

**File:** rs/execution_environment/src/execution_environment/tests.rs (L4240-4244)
```rust
    if let RequestOrResponse::Response(msg) = message {
        assert_eq!(msg.originator, a_id);
        assert_eq!(msg.respondent, b_id);
        assert_eq!(msg.refund, transferred_cycles);
        assert!(matches!(msg.response_payload, Payload::Data(..)));
```
