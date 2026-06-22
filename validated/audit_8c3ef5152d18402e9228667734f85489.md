### Title
Unbounded `raw_rand_contexts` Queue Draining Blocks Entire Execution Round Without Instruction Limit - (File: `rs/execution_environment/src/scheduler.rs`)

---

### Summary

The IC scheduler drains the entire `raw_rand_contexts` queue every round with no per-round instruction cap and no bound on queue size. Any canister that can call `ic0.raw_rand` (i.e., any canister on the subnet) can flood this queue across many rounds. Because each `raw_rand` response is dispatched via `execute_subnet_message` inside an unbounded `while` loop that explicitly bypasses the round instruction limit, a large enough queue causes the scheduler to spend the entire round processing `raw_rand` responses, starving all other canisters of execution time for that round.

---

### Finding Description

In `rs/execution_environment/src/scheduler.rs`, the `execute_round` function contains two separate unbounded drain loops that explicitly skip the per-round instruction limit:

**Loop 1 — consensus queue** (lines 1295–1325):
```rust
// The consensus queue has to be emptied in each round, so we process
// it fully without applying the per-round instruction limit.
while let Some(response) = state.consensus_queue.pop() {
    let (new_state, _) = self.execute_subnet_message(...);
    state = new_state;
}
```

**Loop 2 — `raw_rand_contexts` queue** (lines 1379–1402):
```rust
// Raw rand is not consuming instructions, so all existing raw_rand requests
// will be processed.
while let Some(raw_rand_context) = state
    .metadata
    .subnet_call_context_manager
    .raw_rand_contexts
    .pop_front()
{
    let (new_state, _) = self.execute_subnet_message(...);
    state = new_state;
}
```

The `raw_rand_contexts` queue (`VecDeque<RawRandContext>` in `SubnetCallContextManager`) has **no enforced maximum size**. The `push_raw_rand_request` function appends unconditionally:

```rust
pub fn push_raw_rand_request(
    &mut self,
    request: Request,
    execution_round_id: ExecutionRound,
    time: Time,
) {
    self.raw_rand_contexts.push_back(RawRandContext { request, execution_round_id, time });
}
```

The comment in the scheduler explicitly states the rationale: "Raw rand is not consuming instructions, so all existing raw_rand requests will be processed." While each individual `raw_rand` call consumes zero Wasm instructions, each call to `execute_subnet_message` still performs real work: it invokes the full subnet message dispatch path, generates 32 bytes of randomness via the CSPRNG, serializes a response, and enqueues it into the canister's output queue. With thousands of queued contexts, this loop occupies the entire round's wall-clock time, preventing any canister messages from being executed.

The `raw_rand` call is available to any canister on the subnet (it is a standard management canister method). A malicious canister can issue many `raw_rand` calls in rapid succession across multiple rounds, accumulating a large backlog in `raw_rand_contexts`. Because the queue is drained fully each round without a count limit, the attacker can sustain a state where every round is consumed by `raw_rand` processing.

---

### Impact Explanation

- **Execution liveness denial**: All canister message execution (ingress, inter-canister, heartbeats, timers) is blocked for every round in which the `raw_rand_contexts` queue is large. The inner round loop (`inner_round`) is only reached after both unbounded drain loops complete.
- **Subnet-wide scope**: The effect applies to all canisters on the targeted subnet, not just the attacker's canister.
- **Persistent**: The attacker can continuously refill the queue each round by issuing new `raw_rand` calls, maintaining the denial-of-service indefinitely as long as they have cycles to pay for the calls.
- **No recovery path**: There is no operator mechanism to flush or cap the `raw_rand_contexts` queue without a replica upgrade.

---

### Likelihood Explanation

Any canister deployed on the subnet can call `ic0.raw_rand` (via `ic00::RawRand`). The cost of a `raw_rand` call is the standard management canister call fee. An attacker with sufficient cycles can issue thousands of `raw_rand` calls per round. Because the queue is drained fully each round, the attacker only needs to maintain a queue size large enough to consume the round's wall-clock budget. This is a realistic, low-privilege attack requiring only a deployed canister and cycles.

---

### Recommendation

1. **Enforce a maximum `raw_rand_contexts` queue size**: Reject new `raw_rand` requests when the queue exceeds a configurable bound (e.g., 500 or 1000 entries), returning an error to the caller.
2. **Process `raw_rand` contexts in bounded batches per round**: Replace the unbounded `while` loop with a loop that processes at most `N` entries per round, carrying the remainder to the next round.
3. **Charge cycles proportional to queue depth or add a per-call fee premium** to make flooding economically infeasible.

---

### Proof of Concept

**Root cause — unbounded queue, no size cap:** [1](#0-0) 

**Root cause — unbounded drain loop with explicit bypass of instruction limit:** [2](#0-1) 

**Confirmation that `raw_rand` consumes zero instructions (making the loop truly unbounded by design):** [3](#0-2) 

**`raw_rand_contexts` is an unbounded `VecDeque` in `SubnetCallContextManager`:** [4](#0-3) 

**Test confirming zero instructions consumed per `raw_rand` execution:** [5](#0-4) 

**Attack path:**
1. Attacker deploys a canister on the target subnet.
2. Each round, the canister issues as many `ic00::RawRand` calls as the subnet's per-canister output queue allows.
3. These calls are processed by the subnet message handler, which defers them to `raw_rand_contexts` via `push_raw_rand_request`.
4. The next round's `execute_round` drains the entire `raw_rand_contexts` queue in the unbounded loop before `inner_round` is ever called.
5. With a sufficiently large queue, the round's wall-clock budget is exhausted processing `raw_rand` responses, and no canister messages are executed.
6. The attacker repeats each round, maintaining the backlog.

### Citations

**File:** rs/replicated_state/src/metadata_state/subnet_call_context_manager.rs (L228-229)
```rust
    pub raw_rand_contexts: VecDeque<RawRandContext>,
    pub pre_signature_stashes: BTreeMap<IDkgMasterPublicKeyId, PreSignatureStash>,
```

**File:** rs/replicated_state/src/metadata_state/subnet_call_context_manager.rs (L407-418)
```rust
    pub fn push_raw_rand_request(
        &mut self,
        request: Request,
        execution_round_id: ExecutionRound,
        time: Time,
    ) {
        self.raw_rand_contexts.push_back(RawRandContext {
            request,
            execution_round_id,
            time,
        });
    }
```

**File:** rs/execution_environment/src/scheduler.rs (L1379-1403)
```rust
            // Each round, we check for any postponed `raw_rand` requests.
            // If found, they are processed immediately. Raw rand is not
            // consuming instructions, so all existing raw_rand requests
            // will be processed.
            while let Some(raw_rand_context) = state
                .metadata
                .subnet_call_context_manager
                .raw_rand_contexts
                .pop_front()
            {
                debug_assert_lt!(raw_rand_context.execution_round_id, current_round);
                let (new_state, _) = self.execute_subnet_message(
                    SubnetMessage::Request(raw_rand_context.request.into()),
                    state,
                    &mut csprng,
                    current_round,
                    &mut subnet_round_limits,
                    registry_settings,
                    replica_version,
                    &measurement_scope,
                    &chain_key_data,
                );
                state = new_state;
            }
            scheduler_round_limits.update_subnet_round_limits(&subnet_round_limits);
```

**File:** rs/execution_environment/src/scheduler/tests/subnet_messages.rs (L239-246)
```rust
    assert_eq!(
        fetch_histogram_vec_stats(
            test.metrics_registry(),
            "execution_round_phase_instructions",
        )
        .get(&labels(&[("phase", "raw_rand")])),
        Some(&HistogramStats { count: 1, sum: 0.0 })
    );
```
