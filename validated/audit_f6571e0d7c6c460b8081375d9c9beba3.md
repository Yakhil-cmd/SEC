### Title
Cycles Attached to Inter-Canister Calls Targeting Query Methods Are Not Refunded to Caller - (`rs/execution_environment/tests/hypervisor.rs`)

### Summary

When an update canister attaches cycles to an inter-canister call targeting a query method, those cycles are silently lost rather than refunded to the caller. This is an acknowledged, unfixed cycles accounting bug in the IC execution environment, directly analogous to the Vyper `raw_call` bug where `value` is silently ignored in `delegatecall`/`staticcall`.

### Finding Description

The IC execution environment allows a canister in an update context to use `ic0.call_cycles_add` / `ic0.call_cycles_add128` followed by `ic0.call_perform` to attach cycles to a call targeting another canister's query method. The call is accepted and dispatched, but the cycles are never properly returned to the caller's balance.

The bug is explicitly acknowledged in the codebase with a `TODO(RUN-175)` marker and the test is `#[ignore]`d: [1](#0-0) 

The test documents the expected behavior: canister A (update) calls canister B's query method with cycles attached; since query methods cannot transfer cycles to themselves in this path, the full amount should be refunded to A. Instead, the cycles are lost.

A parallel test in the execution environment test suite confirms that the response message from the query callee *does* carry the refund field set to the transferred amount: [2](#0-1) 

This means the refund is present in the response, but it is not properly credited back to the caller's balance when the reply callback executes.

The composite query path compounds this: `handle_request` in `query_context.rs` always constructs responses with `refund: Cycles::zero()`, unconditionally discarding any cycles that were attached to the outgoing call: [3](#0-2) 

The `action_to_result` function treats any non-zero refund in a query response as a critical bug and increments an error counter rather than processing it: [4](#0-3) 

The `ic0_call_cycles_add_helper` correctly forbids cycle attachment in `CompositeQuery`, `CompositeReplyCallback`, and `CompositeRejectCallback` contexts: [5](#0-4) 

However, the same restriction does not apply to the regular `Update` → `Query` inter-canister call path, where `ic0_call_cycles_add` is permitted and the cycles are deducted from the caller's balance but never returned.

### Impact Explanation

A canister developer who calls a query method with cycles attached (e.g., to implement a multicall pattern where some callees are queries and some are updates) will silently lose those cycles. The caller's balance is debited at `ic0.call_cycles_add` time, the callee cannot accept them (or the refund is not propagated), and the cycles are destroyed. This disrupts cycles accounting and can cause permanent loss of funds for any canister implementing such patterns.

### Likelihood Explanation

Any update canister can trigger this by calling `ic0.call_new` targeting a query method, then calling `ic0.call_cycles_add128` with a nonzero amount, then `ic0.call_perform`. This is a normal, permitted sequence of System API calls. No privileged access is required. The bug is reachable by any unprivileged canister developer.

### Recommendation

The execution environment should either:
1. Reject `ic0.call_perform` (or `ic0.call_cycles_add`) when the target method resolves to a query method and cycles are attached, returning an error analogous to Solidity's compile-time rejection of `delegatecall{value: N}`, or
2. Ensure the full cycle amount is unconditionally refunded to the caller's balance when the response from a query callee is processed, regardless of whether the callee accepted any cycles.

### Proof of Concept

The IC's own test suite provides the proof of concept. The ignored test `cycles_are_refunded_if_callee_is_a_query` in `rs/execution_environment/tests/hypervisor.rs` (lines 6036–6082) demonstrates the exact scenario: canister A attaches cycles to a call to canister B's query method; the test asserts A should receive a full refund, but the assertion fails (hence `#[ignore]`). The companion passing test `replicated_query_refunds_all_sent_cycles` confirms the response message carries the refund, proving the loss occurs in the reply-callback credit path, not in the response generation. [1](#0-0) [6](#0-5) [7](#0-6)

### Citations

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

**File:** rs/execution_environment/src/execution_environment/tests.rs (L4204-4277)
```rust
#[test]
fn replicated_query_refunds_all_sent_cycles() {
    let mut test = ExecutionTestBuilder::new().with_manual_execution().build();
    let initial_cycles = Cycles::new(1_000_000_000_000);
    let a_id = test.universal_canister_with_cycles(initial_cycles).unwrap();
    let b_id = test.universal_canister_with_cycles(initial_cycles).unwrap();
    let transferred_cycles = Cycles::from(1_000_000_u128);

    let b_callback = wasm().message_payload().append_and_reply().build();

    let a_payload = wasm()
        .call_with_cycles(
            b_id,
            "query",
            call_args().other_side(b_callback.clone()),
            transferred_cycles,
        )
        .build();

    let (message_id, _) = test.ingress_raw(a_id, "update", a_payload);

    test.execute_message(a_id);
    test.induct_messages();
    test.execute_message(b_id);

    let system_state = &mut test.canister_state_mut(b_id).system_state;

    assert_eq!(1, system_state.queues().output_queues_len());
    assert_eq!(1, system_state.queues().output_queues_message_count());

    let message = system_state
        .queues_mut()
        .clone()
        .pop_canister_output(&a_id)
        .unwrap();

    if let RequestOrResponse::Response(msg) = message {
        assert_eq!(msg.originator, a_id);
        assert_eq!(msg.respondent, b_id);
        assert_eq!(msg.refund, transferred_cycles);
        assert!(matches!(msg.response_payload, Payload::Data(..)));
    } else {
        panic!("unexpected message popped: {message:?}");
    }

    test.induct_messages();
    test.execute_message(a_id);

    let ingress_state = test.ingress_state(&message_id);

    if let IngressState::Completed(wasm_result) = ingress_state {
        match wasm_result {
            WasmResult::Reject(result) => panic!("unexpected result {result}"),
            WasmResult::Reply(_) => (),
        }
    } else {
        panic!("unexpected ingress state {ingress_state:?}");
    }

    // Canister A gets a refund for all transferred cycles.
    assert_eq!(
        test.canister_state(a_id).system_state.balance(),
        initial_cycles
            - test.canister_execution_cost(a_id).real()
            - test.call_fee("query", &b_callback).real()
            - test.reply_fee(&b_callback).real()
    );

    // Canister B doesn't get the transferred cycles.
    assert_eq!(
        test.canister_state(b_id).system_state.balance(),
        initial_cycles - test.canister_execution_cost(b_id).real()
    );
}
```

**File:** rs/execution_environment/src/query_handler/query_context.rs (L806-815)
```rust
        let to_query_result = |payload: Payload| {
            QueryResponse::CanisterResponse(Response {
                originator: request.sender,
                respondent: request.receiver,
                originator_reply_callback: request.sender_reply_callback,
                response_payload: payload,
                refund: Cycles::zero(),
                deadline: request.deadline,
            })
        };
```

**File:** rs/execution_environment/src/query_handler/query_context.rs (L939-948)
```rust
        if !refund.is_zero() {
            error!(
                self.log,
                "[EXC-BUG] Canister {} refunded {} in a response to a query call. This is a bug @{}",
                canister_id,
                refund,
                QUERY_HANDLER_CRITICAL_ERROR
            );
            self.query_critical_error.inc();
        }
```

**File:** rs/embedders/src/wasmtime_embedder/system_api.rs (L1526-1573)
```rust
    fn ic0_call_cycles_add_helper(
        &mut self,
        method_name: &str,
        amount: Cycles,
    ) -> HypervisorResult<()> {
        match &mut self.api_type {
            ApiType::Start { .. }
            | ApiType::Init { .. }
            | ApiType::ReplicatedQuery { .. }
            | ApiType::Cleanup { .. }
            | ApiType::CompositeCleanup { .. }
            | ApiType::PreUpgrade { .. }
            | ApiType::NonReplicatedQuery { .. }
            | ApiType::CompositeQuery { .. }
            | ApiType::InspectMessage { .. }
            | ApiType::CompositeReplyCallback { .. }
            | ApiType::CompositeRejectCallback { .. } => Err(self.error_for(method_name)),
            ApiType::Update {
                outgoing_request, ..
            }
            | ApiType::SystemTask {
                outgoing_request, ..
            }
            | ApiType::ReplyCallback {
                outgoing_request, ..
            }
            | ApiType::RejectCallback {
                outgoing_request, ..
            } => {
                match outgoing_request {
                    None => Err(HypervisorError::ToolchainContractViolation {
                        error: format!("{method_name} called when no call is under construction."),
                    }),
                    Some(request) => {
                        self.sandbox_safe_system_state
                            .withdraw_cycles_for_transfer(
                                self.memory_usage.current_usage,
                                self.memory_usage.current_message_usage,
                                amount,
                                false, // synchronous error => no need to reveal top up balance
                            )?;
                        request.add_cycles(amount);
                        Ok(())
                    }
                }
            }
        }
    }
```
