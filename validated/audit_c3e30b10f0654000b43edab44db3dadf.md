### Title
Canister `canister_inspect_message` Execution Consumes Subnet Query Resources Without Cycles Charge - (File: `rs/execution_environment/src/execution/inspect_message.rs`)

### Summary
Any unprivileged ingress sender can trigger a canister's `canister_inspect_message` hook, which executes up to 200,000,000 instructions on the subnet's shared query execution threads without charging any cycles to the canister or the sender. A canister developer can deploy a canister with a computationally expensive `canister_inspect_message` that any external user can repeatedly trigger, consuming subnet query execution resources for free.

### Finding Description

The `execute_inspect_message` function runs the canister's `canister_inspect_message` system method in non-replicated mode. The instruction limit is `MAX_INSTRUCTIONS_FOR_MESSAGE_ACCEPTANCE_CALLS = 200_000_000` instructions. [1](#0-0) 

The critical issue is that the `_system_state_accessor` returned by `hypervisor.execute()` is **discarded**, meaning no cycles are ever charged to the canister for this execution: [2](#0-1) 

The `should_accept_ingress_message` function invokes `execute_inspect_message` with `ExecutionMode::NonReplicated` and uses the full subnet memory capacity (no resource reservation): [3](#0-2) 

This execution is scheduled on the subnet's shared query execution threads via `IngressFilterServiceImpl`: [4](#0-3) 

The ingress induction cost check that precedes `execute_inspect_message` is only a balance check, not an actual charge: [5](#0-4) 

If `canister_inspect_message` rejects the message, the canister is never charged for ingress induction either. The result is that up to 200M instructions of Wasm execution can be triggered by any external user with zero cycles cost to anyone.

### Impact Explanation

A canister developer deploys a canister whose `canister_inspect_message` exhausts the full 200M-instruction budget (e.g., a tight spin loop). Any unprivileged user can then spam ingress messages to this canister. Each message causes 200M instructions of execution on the subnet's query scheduler threads — the same thread pool used for legitimate user queries. Since no cycles are charged and the canister pays nothing when the message is rejected, the attacker can continuously consume query execution capacity at zero cost. This degrades query throughput for all users on the subnet. The attack is analogous to the report's finding (1): a canister developer controls the `canister_inspect_message` code (analogous to `isValidSignature`), and any external party can trigger it. [6](#0-5) 

### Likelihood Explanation

The attack requires only: (a) deploying a canister with an expensive `canister_inspect_message`, and (b) sending ingress messages to it. Both steps are available to any unprivileged actor. The canister developer pays no ongoing cost once deployed. The ingress sender pays no cycles (the canister would pay ingress induction only if the message is accepted, which it never is in the attack scenario). The `MAX_INSTRUCTIONS_FOR_MESSAGE_ACCEPTANCE_CALLS` limit of 200M instructions is enforced, but 200M instructions per triggered message is still significant when multiplied across many concurrent senders. [1](#0-0) 

### Recommendation

Charge cycles for `canister_inspect_message` execution proportional to instructions used, similar to how regular query execution is charged. Alternatively, significantly lower `MAX_INSTRUCTIONS_FOR_MESSAGE_ACCEPTANCE_CALLS` to reduce the per-message resource impact, or implement rate-limiting per canister for ingress filter executions. At minimum, the instructions consumed by `canister_inspect_message` should count against the subnet's round instruction budget to bound the total damage per round.

### Proof of Concept

1. Deploy a canister with the following `canister_inspect_message` (WAT pseudocode):
   ```wat
   (func (export "canister_inspect_message")
     ;; spin loop consuming ~200M instructions
     (local $i i64)
     (local.set $i (i64.const 0))
     (block $break
       (loop $loop
         (local.set $i (i64.add (local.get $i) (i64.const 1)))
         (br_if $break (i64.ge_u (local.get $i) (i64.const 20000000)))
         (br $loop)
       )
     )
     ;; do NOT call accept_message — message is rejected, canister pays nothing
   )
   ```
2. From any user identity, repeatedly send ingress messages to this canister.
3. Each message triggers `execute_inspect_message` → `hypervisor.execute()` → up to 200M instructions run on the query scheduler thread pool.
4. The `_system_state_accessor` is discarded; no cycles are charged.
5. The message is rejected; no ingress induction fee is charged.
6. Observe degraded query response times for other canisters on the same subnet as the query thread pool is saturated. [2](#0-1) [7](#0-6)

### Citations

**File:** rs/config/src/execution_environment.rs (L115-117)
```rust
/// The maximum number of instructions for inspect_message calls.
const MAX_INSTRUCTIONS_FOR_MESSAGE_ACCEPTANCE_CALLS: NumInstructions =
    NumInstructions::new(200_000_000);
```

**File:** rs/execution_environment/src/execution/inspect_message.rs (L62-96)
```rust
    let system_api = ApiType::inspect_message(
        ingress.sender().get(),
        ingress.method_name().to_string(),
        ingress.arg().to_vec(),
        time,
        ingress.as_sender_info(),
    );
    let mut round_limits = RoundLimits {
        instructions: as_round_instructions(message_instruction_limit),
        subnet_available_memory,
        // No need for downstream calls.
        subnet_available_callbacks: 0,
        // Ignore compute allocation
        compute_allocation_used: 0,
        subnet_memory_reservation: NumBytes::from(0),
    };
    let inspect_message_timer = ingress_filter_metrics
        .inspect_message_duration_seconds
        .start_timer();
    let (output, _output_execution_state, _system_state_accessor) = hypervisor.execute(
        system_api,
        time,
        system_state,
        memory_usage,
        message_memory_usage,
        execution_parameters,
        FuncRef::Method(method),
        execution_state,
        network_topology,
        &mut round_limits,
        state_changes_error,
        &CallTreeMetricsNoOp,
        time,
        subnet_cycles_config,
    );
```

**File:** rs/execution_environment/src/execution_environment.rs (L3340-3374)
```rust
        // A first-pass check on the canister's balance to prevent needless gossiping
        // if the canister's balance is too low. A more rigorous check happens later
        // in the ingress selector.
        {
            let subnet_cycles_config = state.get_own_subnet_cycles_config();
            let induction_cost = self.cycles_account_manager.ingress_induction_cost(
                ingress,
                effective_canister_id,
                subnet_cycles_config,
            );

            if let IngressInductionCost::Fee { payer, cost } = induction_cost {
                let paying_canister = canister(payer)?;
                let reveal_top_up = paying_canister
                    .controllers()
                    .contains(&ingress.sender().get());
                if let Err(err) = self
                    .cycles_account_manager
                    .can_withdraw_cycles_with_threshold(
                        &paying_canister.system_state,
                        cost,
                        paying_canister.memory_usage(),
                        paying_canister.message_memory_usage(),
                        paying_canister.system_state.reserved_balance(),
                        subnet_cycles_config,
                        reveal_top_up,
                    )
                {
                    return Err(UserError::new(
                        ErrorCode::CanisterOutOfCycles,
                        err.to_string(),
                    ));
                }
            }
        }
```

**File:** rs/execution_environment/src/execution_environment.rs (L3412-3444)
```rust
        // An inspect message is expected to finish quickly, so DTS is not
        // supported for it.
        let instruction_limits = InstructionLimits::new(
            self.config.max_instructions_for_message_acceptance_calls,
            self.config.max_instructions_for_message_acceptance_calls,
        );

        // Letting the canister grow arbitrarily when executing the
        // query is fine as we do not persist state modifications.
        let subnet_available_memory =
            full_subnet_memory_capacity(&self.config, state.resource_limits());
        let execution_parameters = self.execution_parameters(
            canister_state,
            instruction_limits,
            execution_mode,
            // Effectively disable subnet memory resource reservation for queries.
            ResourceSaturation::default(),
        );

        inspect_message::execute_inspect_message(
            state.time(),
            canister_state.clone(),
            ingress.content(),
            execution_parameters,
            subnet_available_memory,
            &self.hypervisor,
            &state.metadata.network_topology,
            &self.log,
            &self.metrics.state_changes_error,
            metrics,
            state.get_own_subnet_cycles_config(),
        )
        .1
```

**File:** rs/execution_environment/src/ingress_filter.rs (L51-77)
```rust
    fn call(&mut self, (provisional_whitelist, raw_ingress): IngressFilterInput) -> Self::Future {
        let exec_env = Arc::clone(&self.exec_env);
        let metrics = Arc::clone(&self.metrics);
        let state_reader = Arc::clone(&self.state_reader);
        let (tx, rx) = oneshot::channel();
        let canister_id = raw_ingress.content().canister_id();
        self.query_scheduler.push(canister_id, move || {
            let start = std::time::Instant::now();
            if !tx.is_closed() {
                let result = match state_reader.get_latest_certified_state() {
                    Some(state) => {
                        let v = exec_env.should_accept_ingress_message(
                            state.take(),
                            &provisional_whitelist,
                            &raw_ingress,
                            ExecutionMode::NonReplicated,
                            &metrics,
                        );
                        Ok(v)
                    }
                    None => Err(IngressFilterError::CertifiedStateUnavailable),
                };

                let _ = tx.send(Ok(result));
            }
            start.elapsed()
        });
```
