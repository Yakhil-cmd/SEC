### Title
Unbounded `raw_rand_contexts` Queue Drained Entirely Each Round Without Size Limit - (File: rs/execution_environment/src/scheduler.rs)

### Summary

The IC scheduler drains the entire `raw_rand_contexts` queue in every execution round without any size cap on the queue itself. Any canister can enqueue `RawRand` requests, and the scheduler comment explicitly states "Raw rand is not consuming instructions, so **all** existing raw_rand requests will be processed." There is no maximum size enforced on `raw_rand_contexts` before or during enqueue. An attacker controlling a canister can flood this queue, causing the scheduler to spend unbounded wall-clock time in the raw_rand drain loop each round, degrading or stalling subnet progress — the IC analog of the CoreDAO unbounded candidate-set DoS.

### Finding Description

`SubnetCallContextManager` holds a `VecDeque<RawRandContext>` named `raw_rand_contexts`. [1](#0-0) 

The only write path is `push_raw_rand_request`, which appends unconditionally with no size check: [2](#0-1) 

Every execution round, the scheduler drains the **entire** queue in a `while let Some(...)` loop. The inline comment confirms the design intent — no instruction limit is applied: [3](#0-2) 

Each iteration calls `execute_subnet_message`, which is a non-trivial operation (state mutation, response generation, ingress history write). With N entries in the queue, the scheduler performs N such calls before any canister messages are executed. There is no bound on N.

Contrast this with the analogous `canister_http_request_contexts`, which enforces `MAX_CANISTER_HTTP_REQUESTS_IN_FLIGHT = 3000` before accepting a new entry: [4](#0-3) 

And `sign_with_threshold_contexts`, which enforces a per-key `dynamic_queue_size` before accepting: [5](#0-4) 

No equivalent guard exists for `raw_rand_contexts`.

### Impact Explanation

A malicious canister can issue a large number of `RawRand` calls in rapid succession (each call is cheap — zero cycles on system subnets, and the canister queue capacity of 500 per pair limits per-round induction, but across many rounds the queue accumulates). Once the `raw_rand_contexts` queue grows large, every subsequent execution round must drain it entirely before processing any canister messages. This can:

1. Severely delay or stall canister message execution on the affected subnet for multiple rounds.
2. Cause the subnet's round wall-clock time to balloon, degrading liveness for all users of that subnet.
3. In an extreme case, if the queue grows large enough that draining it exceeds the block time budget, the subnet's progress rate drops to near zero — a practical denial of service.

**Impact: 4/5** — Subnet-wide liveness degradation affecting all canisters on the subnet.

### Likelihood Explanation

Any canister on the subnet can call `ic0.raw_rand` (via `ic00::RawRand`). The call is free on system subnets and costs only cycles on application subnets. The canister output queue capacity (500 per pair) limits how fast a single canister can inject requests per round, but across many rounds the queue accumulates without bound. A well-resourced attacker with a canister holding sufficient cycles can sustain the attack indefinitely.

**Likelihood: 2/5** — Requires a funded canister and sustained effort, but no privileged access.

### Recommendation

Add a maximum size cap to `raw_rand_contexts` analogous to `MAX_CANISTER_HTTP_REQUESTS_IN_FLIGHT`. Before calling `push_raw_rand_request`, check the current queue length and reject (with a `CanisterRejectedMessage` error) if the limit is exceeded. A reasonable limit (e.g., 500–1000) would bound the per-round drain cost while still serving legitimate use cases. Alternatively, process only a bounded batch of `raw_rand_contexts` per round rather than draining the entire queue.

### Proof of Concept

1. Deploy a canister on an application subnet with sufficient cycles.
2. In a loop spanning many rounds, have the canister call `ic00::RawRand` as fast as possible. Each round, up to 500 requests (the output queue capacity) are inducted into the subnet queue and processed into `raw_rand_contexts` for the next round.
3. After N rounds, `raw_rand_contexts.len()` approaches N × (requests inducted per round).
4. Each subsequent round, the scheduler's `while let Some(raw_rand_context) = ... .pop_front()` loop at `rs/execution_environment/src/scheduler.rs:1383` iterates over all accumulated entries before any canister messages run.
5. Observe that canister message latency on the subnet increases proportionally to the queue size, and that the `round_postponed_raw_rand_queue` metric duration grows without bound.

The root cause — no size limit on `raw_rand_contexts` — is confirmed by the absence of any capacity check in `push_raw_rand_request` at `rs/replicated_state/src/metadata_state/subnet_call_context_manager.rs:407–418`, in contrast to the explicit guards present for HTTP outcall and threshold-signature contexts. [2](#0-1) [6](#0-5)

### Citations

**File:** rs/replicated_state/src/metadata_state/subnet_call_context_manager.rs (L228-228)
```rust
    pub raw_rand_contexts: VecDeque<RawRandContext>,
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

**File:** rs/execution_environment/src/scheduler.rs (L1370-1404)
```rust
        // Execute postponed `raw_rand` subnet messages.
        {
            // Drain the queue holding postponed `raw_rand`` queue.
            let measurement_scope = MeasurementScope::nested(
                &self.metrics.round_postponed_raw_rand_queue,
                &root_measurement_scope,
            );
            let mut subnet_round_limits = scheduler_round_limits.subnet_round_limits();

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
        }
```

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

**File:** rs/execution_environment/src/execution_environment.rs (L2838-2858)
```rust
        )
    }

    fn clear_chunk_store(
        &self,
        sender: PrincipalId,
        state: &mut ReplicatedState,
        msg: &mut CanisterCall,
        args: ClearChunkStoreArgs,
        round_limits: &mut RoundLimits,
        resource_saturation: &ResourceSaturation,
        current_round: ExecutionRound,
    ) -> ExecuteSubnetMessageResult {
        let subnet_cycles_config = state.get_own_subnet_cycles_config();
        let canister_id = args.get_canister_id();
        self.execute_mgmt_operation_on_canister(
            canister_id,
            |canister, _msg, round_limits, _consumed_cycles| {
                self.canister_manager.clear_chunk_store(
                    sender,
                    canister,
```
