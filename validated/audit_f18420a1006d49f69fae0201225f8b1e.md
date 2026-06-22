### Title
Malicious Callee Canister Can Exhaust Shared Instruction Budget to Block Composite Queries - (File: rs/execution_environment/src/query_handler/query_call_graph.rs)

### Summary
The composite query execution framework maintains a single shared instruction budget (`round_limits.instructions`) across the entire call graph. Because the per-sub-call instruction limit (`max_instructions_per_query = 5 B`) equals the total graph budget (`max_query_call_graph_instructions = 5 B`), a single malicious callee canister can exhaust the entire budget in one execution, causing `evaluate_query_call_graph` to abort the whole composite query with `QueryCallGraphTotalInstructionLimitExceeded`. Any legitimate canister that fans out composite queries to user-controlled canisters is vulnerable to this denial-of-service.

### Finding Description
`evaluate_query_call_graph` in `rs/execution_environment/src/query_handler/query_call_graph.rs` performs a DFS over the composite query call graph. At the top of every loop iteration it checks whether the shared budget is exhausted:

```rust
if query_context.instruction_limit_reached() {
    let error = UserError::new(
        ErrorCode::QueryCallGraphTotalInstructionLimitExceeded,
        "Composite query calls exceeded the instruction limit.",
    );
    return QueryResponse::UserError(error);   // entire query fails
}
``` [1](#0-0) 

The per-sub-call instruction limit is computed in `execute_query` as:

```rust
let instruction_limit = self.max_instructions_per_query.min(NumInstructions::new(
    self.round_limits.instructions.get().max(0) as u64,
));
``` [2](#0-1) 

`max_instructions_per_query` is `MAX_INSTRUCTIONS_PER_QUERY_MESSAGE = 5 000 000 000`: [3](#0-2) 

The total graph budget is `MAX_INSTRUCTIONS_PER_COMPOSITE_QUERY_CALL = 5 000 000 000`: [4](#0-3) 

Both constants are identical. A single sub-call is therefore permitted to consume the entire graph budget. After that sub-call returns, `round_limits.instructions` is at or below zero, `instruction_limit_reached()` returns `true` on the very next loop iteration, and the whole composite query is terminated as a `UserError` — even though subsequent callees in the graph have not yet been attempted.

The per-call overhead deduction (`INSTRUCTION_OVERHEAD_PER_QUERY_CALL = 50 000 000`) is applied *after* execution and does not prevent a single callee from consuming the full 5 B budget: [5](#0-4) [6](#0-5) 

The existing test `composite_query_callgraph_max_instructions_is_enforced` confirms that exceeding the graph budget causes the entire query to fail with `QueryCallGraphTotalInstructionLimitExceeded`, not just the offending sub-call: [7](#0-6) 

### Impact Explanation
Any canister that fans out composite queries to a set of canisters it does not fully control (e.g., a DEX aggregator querying registered token canisters, a portfolio tracker querying user-deployed asset canisters, or any open registry pattern) can be permanently blocked. The attacker deploys one malicious canister that spins a tight Wasm loop until its instruction allowance is consumed. Once that canister is included in the composite query call graph — even as the first callee — the entire query fails and no subsequent canisters are reached. The legitimate canister's composite query endpoint becomes permanently non-functional for any query that includes the poison canister.

### Likelihood Explanation
The attack is realistic wherever a canister dynamically builds its composite query call graph from a registry or list that any principal can extend. The attacker pays only the one-time cost of deploying a canister; the attack itself costs nothing per query because the malicious canister's instructions are charged to the query caller, not the attacker. The attack is persistent: the malicious canister remains deployed indefinitely. The entry path is a standard unprivileged canister deployment followed by registration in the target's registry — no privileged access is required.

### Recommendation
Decouple the per-sub-call instruction limit from the total graph budget. For example, cap each sub-call at `max_query_call_graph_instructions / MAX_QUERY_CALL_DEPTH` (≈ 833 M with current defaults), so that no single callee can exhaust the entire shared pool. Alternatively, expose a per-sub-call cap as a configurable parameter distinct from `max_instructions_per_query`, analogous to the audit team's note that "a parameter would do" in the original report.

### Proof of Concept
1. Attacker deploys canister **M** whose query method executes a tight Wasm loop consuming all available instructions before returning.
2. Legitimate canister **A** implements a composite query that sequentially calls canisters `[M, B, C]` (M is first because it advertises the lowest fee / highest priority in A's registry).
3. User sends a composite query to **A**.
4. **A** calls **M**; **M** consumes all 5 B instructions from `round_limits.instructions`.
5. On the next DFS iteration, `instruction_limit_reached()` is `true`; `evaluate_query_call_graph` returns `QueryResponse::UserError(QueryCallGraphTotalInstructionLimitExceeded)`.
6. Canisters **B** and **C** are never called; the user receives an error.
7. Every subsequent composite query to **A** that includes **M** fails identically — at zero cost to the attacker. [8](#0-7) [9](#0-8)

### Citations

**File:** rs/execution_environment/src/query_handler/query_call_graph.rs (L61-86)
```rust
    while let Some(PendingCall(canister, call_origin, mut requests)) = call_stack.pop() {
        // Loop invariant: `callee_result` is a result of a query call made by
        // `(canister, call_origin)`.

        // First check the DFS limits.
        if call_stack.len() >= max_query_call_graph_depth {
            let error = UserError::new(
                ErrorCode::QueryCallGraphTooDeep,
                "Composite query calls exceeded the maximum call depth.",
            );
            return QueryResponse::UserError(error);
        }
        if query_context.instruction_limit_reached() {
            let error = UserError::new(
                ErrorCode::QueryCallGraphTotalInstructionLimitExceeded,
                "Composite query calls exceeded the instruction limit.",
            );
            return QueryResponse::UserError(error);
        }
        if query_context.time_limit_reached() {
            let error = UserError::new(
                ErrorCode::QueryTimeLimitExceeded,
                "Composite query call exceeded the time limit.",
            );
            return QueryResponse::UserError(error);
        }
```

**File:** rs/execution_environment/src/query_handler/query_context.rs (L425-428)
```rust
        let instruction_limit = self.max_instructions_per_query.min(NumInstructions::new(
            self.round_limits.instructions.get().max(0) as u64,
        ));
        let instruction_limits = InstructionLimits::new(instruction_limit, instruction_limit);
```

**File:** rs/execution_environment/src/query_handler/query_context.rs (L857-857)
```rust
        self.round_limits.instructions -= self.instruction_overhead_per_query_call;
```

**File:** rs/config/src/subnet_config.rs (L40-41)
```rust
// Going above the limit results in an `InstructionLimitExceeded` error.
pub const MAX_INSTRUCTIONS_PER_QUERY_MESSAGE: NumInstructions = NumInstructions::new(5 * B);
```

**File:** rs/config/src/execution_environment.rs (L121-122)
```rust
/// Equivalent to MAX_INSTRUCTIONS_PER_MESSAGE_WITHOUT_DTS for now
pub(crate) const MAX_INSTRUCTIONS_PER_COMPOSITE_QUERY_CALL: u64 = 5_000_000_000;
```

**File:** rs/config/src/execution_environment.rs (L126-127)
```rust
/// This would allow 100 calls with the current MAX_INSTRUCTIONS_PER_COMPOSITE_QUERY_CALL
pub const INSTRUCTION_OVERHEAD_PER_QUERY_CALL: u64 = 50_000_000;
```

**File:** rs/execution_environment/src/query_handler/tests.rs (L362-429)
```rust
#[test]
fn composite_query_callgraph_max_instructions_is_enforced() {
    const NUM_CANISTERS: u64 = 20;
    const NUM_SUCCESSFUL_QUERIES: u64 = 5; // Number of calls expected to succeed

    let mut test = ExecutionTestBuilder::new()
        .with_max_query_call_graph_instructions(NumInstructions::from(
            NUM_SUCCESSFUL_QUERIES * INSTRUCTION_OVERHEAD_PER_QUERY_CALL,
        ))
        .build();

    let mut canisters = vec![];
    for _ in 0..NUM_CANISTERS {
        canisters.push(test.universal_canister_with_cycles(CYCLES_BALANCE).unwrap());
    }

    // Generate call tree of depth 1.
    // Canister 0 will call into each canister 1..num_canisters exactly once in a sequential manner.
    // This will therefore *not* hit the call graph depth limit, but should hit a limit
    // on the maximum number of instructions in a call graph.
    fn generate_call_to(
        canisters: &[ic_types::CanisterId],
        canister_idx: usize,
    ) -> ic_universal_canister::PayloadBuilder {
        assert_lt!(canister_idx, canisters.len());

        let reply = if canister_idx <= 1 {
            wasm().stable_size().reply_int()
        } else {
            generate_call_to(canisters, canister_idx - 1)
        };

        wasm().stable_grow(10).composite_query(
            canisters[canister_idx],
            call_args()
                .other_side(wasm().reply_data(b"ignore".as_ref()))
                .on_reply(reply),
        )
    }

    // Those should succeed
    for num_calls in 1..NUM_SUCCESSFUL_QUERIES {
        let test = test.non_replicated_query(
            canisters[0],
            "composite_query",
            generate_call_to(&canisters, num_calls as usize).build(),
        );
        match &test {
            Ok(_) => {}
            Err(err) => panic!(
                "Query with {num_calls} calls failed, when it should have succeeded: {err:?}"
            ),
        }
    }
    for num_calls in NUM_SUCCESSFUL_QUERIES..NUM_CANISTERS {
        let test = test.non_replicated_query(
            canisters[0],
            "composite_query",
            generate_call_to(&canisters, num_calls as usize).build(),
        );
        match &test {
            Ok(_) => panic!("Query with {num_calls} calls should have failed!"),
            Err(err) => assert_eq!(
                err.code(),
                ErrorCode::QueryCallGraphTotalInstructionLimitExceeded
            ),
        }
    }
```
