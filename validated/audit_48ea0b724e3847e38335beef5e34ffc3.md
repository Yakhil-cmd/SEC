I have now traced the full code path. Let me verify the `OutputRequest` struct and the `push_output_request` sender assertion before finalizing.

### Title
Compromised Sandbox Can Inject Arbitrary `OutputRequest` Entries via Unvalidated `SystemStateModifications.requests` — (`rs/embedders/src/wasmtime_embedder/system_api/sandbox_safe_system_state.rs`)

---

### Summary

The replica controller accepts `SystemStateModifications` from the sandbox process over IPC and applies the `requests` field directly to the canister's output queue without verifying that those requests were actually initiated by Wasm execution via `ic0.call_*` system API calls. A compromised sandbox process can craft a forged `ExecutionFinishedRequest` containing arbitrary `OutputRequest` entries, causing the replica to enqueue unauthorized inter-canister calls on behalf of the canister.

---

### Finding Description

**Full call chain:**

**Step 1 — Sandbox sends forged `ExecutionFinishedRequest`.**
The sandbox process calls `controller.execution_finished(ExecutionFinishedRequest { exec_id, exec_output })` over the Unix domain socket IPC channel. The `exec_output.state.system_state_modifications.requests` field is fully attacker-controlled. [1](#0-0) 

**Step 2 — Replica controller passes it through without validation.**
`ControllerServiceImpl::execution_finished` looks up the completion closure by `exec_id` and calls it with the raw `exec_output`. There is no inspection or validation of `system_state_modifications.requests`. [2](#0-1) 

**Step 3 — `process_completion` → `update_execution_state` passes `system_state_modifications` through unchanged.**
`update_execution_state` extracts `system_state_modifications` from the sandbox output and wraps it in `CanisterStateChanges` without any validation of the `requests` field. [3](#0-2) 

**Step 4 — `validate_cycle_change` only checks internal consistency, not request origin.**
The only guard in `apply_changes` is `validate_cycle_change`, which verifies that `cycles_balance_change == sum(req.payment) + consumed + reserved`. A compromised sandbox sets `payment = 0` and `cycles_balance_change = CyclesBalanceChange::zero()`, making the check trivially pass. [4](#0-3) 

**Step 5 — `apply_changes` enqueues the forged request.**
For each request in `self.requests`, `apply_changes` calls `push_message` → `system_state.push_output_request(msg, time)`. [5](#0-4) 

**Step 6 — The only sender check is a debug `assert_eq!`, not a security guard.**
`SystemState::push_output_request` asserts `request.sender == self.canister_id`. A compromised sandbox knows the canister ID (it was passed in the execution input) and sets `sender` accordingly, so this assertion passes. [6](#0-5) 

**Step 7 — The `OutputRequest` struct is fully serializable and all fields are attacker-controlled.**
`OutputRequest` is `#[derive(Deserialize, Serialize)]` with public fields including `receiver`, `method_name`, `method_payload`, `payment`, `call_context_id`, and `on_reply`/`on_reject` Wasm closures. [7](#0-6) 

**Step 8 — The in-sandbox `take_system_state_modifications` filtering is irrelevant for a compromised sandbox.**
The filtering logic in `SystemApiImpl::take_system_state_modifications` (which strips `requests` for non-update API types) runs inside the sandbox process. A compromised sandbox bypasses this entirely and sends a hand-crafted `SystemStateModifications` directly over IPC. [8](#0-7) 

---

### Impact Explanation

Once the forged `OutputRequest` is enqueued in the canister's output queue, message routing delivers it to the target canister (e.g., the ICP ledger). The ledger sees a legitimate inter-canister call from the attacker's canister and executes it. If the canister holds ICP tokens, the attacker can call `transfer` to move them to an arbitrary account. The `on_reply`/`on_reject` Wasm closures in the forged request point to attacker-controlled Wasm table indices, enabling further exploitation upon response.

---

### Likelihood Explanation

The precondition is compromise of the sandbox process. This requires exploiting a memory-safety vulnerability in wasmtime or the sandbox binary to escape the Wasm execution environment. This is a non-trivial but realistic attack for a sophisticated adversary targeting a high-value canister. The ICP bug bounty explicitly covers sandbox escape scenarios. Once the sandbox is compromised, the injection is straightforward and deterministic.

---

### Recommendation

The replica controller must re-derive or independently validate `SystemStateModifications.requests` rather than trusting the sandbox. Concretely:

1. **Track outgoing calls on the replica side**: Before dispatching execution to the sandbox, record the set of `ic0.call_perform` invocations via the system API call counters already returned in `WasmExecutionOutput.system_api_call_counters`. Cross-check the number of requests in `system_state_modifications.requests` against `system_api_call_counters.call_perform_count`.
2. **Validate request fields**: At minimum, verify `request.sender == canister_id` as a hard error (not a panic), and verify `request.call_context_id` exists in the canister's call context manager before enqueuing.
3. **Consider moving request construction to the replica side**: The replica could reconstruct `OutputRequest` entries from the system API call log rather than accepting them wholesale from the sandbox.

---

### Proof of Concept

```rust
// In a test or integration harness:
// 1. Create a SystemStateModifications with a forged OutputRequest
let forged_request = OutputRequest {
    sender: attacker_canister_id,       // known from execution input
    receiver: ledger_canister_id,
    method_name: "transfer".to_string(),
    method_payload: encode_transfer_args(attacker_account, amount),
    payment: Cycles::zero(),
    call_context_id: valid_call_context_id, // known from execution input
    // ... zero prepayments, dummy closures
};
let forged_mods = SystemStateModifications {
    requests: vec![forged_request],
    cycles_balance_change: CyclesBalanceChange::zero(), // consistent with payment=0
    ..SystemStateModifications::default()
};

// 2. validate_cycle_change passes: 0 == 0
forged_mods.validate_cycle_change(false).unwrap(); // Ok(())

// 3. apply_changes enqueues the forged request
forged_mods.apply_changes(time, &mut system_state, &topology, subnet_id, false, &metrics, &log).unwrap();

// 4. Verify the forged request appears in the output queue
assert_eq!(1, system_state.queues().output_queues_len());
// Message routing will now deliver this to the ledger.
```

### Citations

**File:** rs/canister_sandbox/src/replica_controller/controller_service_impl.rs (L48-79)
```rust
    fn execution_finished(
        &self,
        req: protocol::ctlsvc::ExecutionFinishedRequest,
    ) -> rpc::Call<protocol::ctlsvc::ExecutionFinishedReply> {
        let exec_id = req.exec_id;
        let exec_output = req.exec_output;
        // Sandbox is telling us that execution has finished for this
        // ID. We will validate this ID by looking up the execution
        // state for this ID and extracting its closure. If the closure
        // is not there, then the sandbox is "buggy" (or worse) and
        // trying to either issue "double-completions" or completions
        // for non-existent executions. Deal with this by ignoring
        // such calls (but log them).
        // Maybe we also want to deal with this in more radical ways
        // (e.g. forcibly terminate the sandbox process).
        let reply = self.registry.take(exec_id).map_or_else(
            || {
                // Should we log the entire erroneous request? It
                // could both be large and hold canister-sensitive
                // data, so maybe this is not advisable.
                error!(
                    self.log,
                    "Wasm sandbox process sent completion for non-existent execution {}", &exec_id
                );
                Err(rpc::Error::ServerError)
            },
            |completion| {
                completion(exec_id, CompletionResult::Finished(exec_output));
                Ok(protocol::ctlsvc::ExecutionFinishedReply {})
            },
        );
        rpc::Call::new_resolved(reply)
```

**File:** rs/canister_sandbox/src/replica_controller/sandboxed_execution_controller.rs (L1760-1827)
```rust
    ) -> CanisterStateChanges {
        let StateModifications {
            execution_state_modifications,
            system_state_modifications,
        } = exec_output.take_state_modifications();

        match execution_state_modifications {
            None => CanisterStateChanges {
                execution_state_changes: None,
                system_state_modifications,
            },
            Some(execution_state_modifications) => {
                // TODO: If a canister has broken out of wasm then it might have allocated more
                // wasm or stable memory then allowed. We should add an additional check here
                // that thet canister is still within it's allowed memory usage.
                let mut wasm_memory = execution_state.wasm_memory.clone();
                wasm_memory
                    .page_map
                    .deserialize_delta(execution_state_modifications.wasm_memory.page_delta);
                wasm_memory.size = execution_state_modifications.wasm_memory.size;
                wasm_memory.sandbox_memory = SandboxMemory::synced(wrap_remote_memory(
                    &sandbox_process,
                    next_wasm_memory_id,
                ));
                if let Err(err) = wasm_memory.verify_size() {
                    error!(
                        self.logger,
                        "{}: Canister {} has invalid wasm memory size: {}",
                        SANDBOXED_EXECUTION_INVALID_MEMORY_SIZE,
                        canister_id,
                        err
                    );
                    self.metrics
                        .sandboxed_execution_critical_error_invalid_memory_size
                        .inc();
                }
                let mut stable_memory = execution_state.stable_memory.clone();
                stable_memory
                    .page_map
                    .deserialize_delta(execution_state_modifications.stable_memory.page_delta);
                stable_memory.size = execution_state_modifications.stable_memory.size;
                stable_memory.sandbox_memory = SandboxMemory::synced(wrap_remote_memory(
                    &sandbox_process,
                    next_stable_memory_id,
                ));
                if let Err(err) = stable_memory.verify_size() {
                    error!(
                        self.logger,
                        "{}: Canister {} has invalid stable memory size: {}",
                        SANDBOXED_EXECUTION_INVALID_MEMORY_SIZE,
                        canister_id,
                        err
                    );
                    self.metrics
                        .sandboxed_execution_critical_error_invalid_memory_size
                        .inc();
                }
                CanisterStateChanges {
                    execution_state_changes: Some(ExecutionStateChanges {
                        globals: execution_state_modifications.globals,
                        wasm_memory,
                        stable_memory,
                    }),
                    system_state_modifications,
                }
            }
        }
    }
```

**File:** rs/embedders/src/wasmtime_embedder/system_api/sandbox_safe_system_state.rs (L160-191)
```rust
    fn validate_cycle_change(&self, is_cmc_canister: bool) -> HypervisorResult<()> {
        let mut expected_change = CyclesBalanceChange::zero();

        if let Some((_, call_context_balance_taken)) = self.call_context_balance_taken {
            expected_change =
                expected_change + CyclesBalanceChange::added(call_context_balance_taken);
        }

        for req in self.requests.iter() {
            expected_change = expected_change + CyclesBalanceChange::removed(req.payment);
        }

        for amount in self.consumed_cycles_by_use_case.iter_over_real() {
            expected_change = expected_change + CyclesBalanceChange::removed(amount);
        }

        expected_change = expected_change + CyclesBalanceChange::removed(self.reserved_cycles);

        // If the canister is not the cycles minting canister, then the balance
        // change coming from the Wasm execution must match the expected balance
        // change that we just computed.
        if is_cmc_canister || self.cycles_balance_change == expected_change {
            Ok(())
        } else {
            Err(HypervisorError::WasmEngineError(
                WasmEngineError::FailedToApplySystemChanges(format!(
                    "Invalid cycle change: expected {:?}, got {:?}",
                    expected_change, self.cycles_balance_change
                )),
            ))
        }
    }
```

**File:** rs/embedders/src/wasmtime_embedder/system_api/sandbox_safe_system_state.rs (L453-531)
```rust
        // Push outgoing messages.
        let nns_subnet_id = network_topology.nns_subnet_id;
        let subnet_ids: BTreeSet<PrincipalId> =
            network_topology.subnets().keys().map(|s| s.get()).collect();
        for mut msg in self.requests {
            if msg.receiver == IC_00 {
                match Self::validate_sender_canister_version(&msg, system_state.canister_version())
                {
                    Ok(()) => {
                        // This is a request to ic:00. Update the receiver to the appropriate subnet.
                        match routing::resolve_destination(
                            network_topology,
                            msg.method_name.as_str(),
                            msg.method_payload.as_slice(),
                            own_subnet_id,
                            system_state.canister_id(),
                            is_composite_query,
                            logger,
                        )
                        .map(CanisterId::unchecked_from_principal)
                        {
                            Ok(destination_subnet) => {
                                msg.receiver = destination_subnet;
                                Self::push_message(system_state, time, msg, logger)?;
                            }
                            Err(err) => {
                                Self::reject_subnet_message_routing(
                                    system_state,
                                    &subnet_ids,
                                    msg,
                                    err,
                                    logger,
                                )?;
                            }
                        }
                    }
                    Err(err) => {
                        Self::reject_subnet_message_user_error(
                            system_state,
                            &subnet_ids,
                            msg,
                            err,
                            logger,
                        )?;
                    }
                }
            } else if subnet_ids.contains(&msg.receiver.get()) {
                match Self::validate_sender_canister_version(&msg, system_state.canister_version())
                {
                    Ok(()) => {
                        if own_subnet_id != nns_subnet_id {
                            // This is a management canister call providing the target subnet ID
                            // directly in the request. This is only allowed for NNS canisters.
                            let err = ResolveDestinationError::AlreadyResolved(msg.receiver.get());
                            Self::reject_subnet_message_routing(
                                system_state,
                                &subnet_ids,
                                msg,
                                err,
                                logger,
                            )?;
                        } else {
                            Self::push_message(system_state, time, msg, logger)?;
                        }
                    }
                    Err(err) => {
                        Self::reject_subnet_message_user_error(
                            system_state,
                            &subnet_ids,
                            msg,
                            err,
                            logger,
                        )?;
                    }
                }
            } else {
                Self::push_message(system_state, time, msg, logger)?;
            }
        }
```

**File:** rs/replicated_state/src/canister_state/system_state.rs (L83-109)
```rust
#[derive(Clone, PartialEq, Debug, Deserialize, Serialize)]
pub struct OutputRequest {
    // Fields shared by `Request` and `Callback`.
    /// The destination canister (`Request::receiver` / `Callback::respondent`).
    pub receiver: CanisterId,
    /// Cycles attached to the request (`Request::payment` /
    /// `Callback::cycles_sent`).
    pub payment: Cycles,
    /// If non-zero, this is a best-effort call (`Request::deadline` /
    /// `Callback::deadline`).
    pub deadline: CoarseTime,

    // `Request`-only fields.
    pub sender: CanisterId,
    pub method_name: String,
    pub method_payload: Vec<u8>,
    pub metadata: RequestMetadata,

    // `Callback`-only fields.
    pub call_context_id: CallContextId,
    pub prepayment_for_response_execution: CompoundCycles<Instructions>,
    pub prepayment_for_response_transmission: CompoundCycles<RequestAndResponseTransmission>,
    pub prepayment_for_call_transmission: CompoundCycles<RequestAndResponseTransmission>,
    pub on_reply: WasmClosure,
    pub on_reject: WasmClosure,
    pub on_cleanup: Option<WasmClosure>,
}
```

**File:** rs/replicated_state/src/canister_state/system_state.rs (L1155-1177)
```rust
    pub fn push_output_request(
        &mut self,
        request: OutputRequest,
        time: Time,
    ) -> Result<CallbackId, StateError> {
        assert_eq!(
            request.sender, self.canister_id,
            "Expected `Request` to have been sent by canister ID {}, but instead got {}",
            self.canister_id, request.sender
        );

        let callback = request.to_callback();
        let callback_id = self.register_callback(callback)?;
        let request = request.into_request(callback_id);
        let result = self.queues.push_output_request(request.into(), time);
        match result {
            Ok(()) => Ok(callback_id),
            Err((err, _msg)) => {
                self.unregister_callback(callback_id)?;
                Err(err)
            }
        }
    }
```

**File:** rs/embedders/src/wasmtime_embedder/system_api.rs (L1703-1819)
```rust
    pub fn take_system_state_modifications(&mut self) -> SystemStateModifications {
        let mut system_state_modifications = self.sandbox_safe_system_state.take_changes();
        // In the below, we explicitly list all fields of `SystemStateModifications`
        // so that an explicit decision needs to be made for each context and
        // and execution result combination when a new field is added to the struct.
        match self.api_type {
            // Inspect message runs in non-replicated mode, not persisting any changes.
            // Same for non-replicated queries.
            ApiType::InspectMessage { .. } | ApiType::NonReplicatedQuery { .. } => {
                SystemStateModifications {
                    new_certified_data: None,
                    cycles_balance_change: CyclesBalanceChange::zero(),
                    reserved_cycles: Cycles::zero(),
                    consumed_cycles_by_use_case: ConsumedCyclesDuringExecution::default(),
                    call_context_balance_taken: None,
                    request_slots_used: BTreeMap::new(),
                    requests: vec![],
                    new_global_timer: None,
                    canister_log: CanisterLog::default_delta(),
                    should_bump_canister_version: false,
                }
            }
            // Composite queries, as well as composite reply, reject and cleanup
            // callbacks should persist any changes related to inter-canister
            // calls, like output queue requests and callbacks.
            // In case of a trap, no changes are returned.
            ApiType::CompositeQuery { .. }
            | ApiType::CompositeReplyCallback { .. }
            | ApiType::CompositeRejectCallback { .. }
            | ApiType::CompositeCleanup { .. } => match &self.execution_error {
                Some(_) => SystemStateModifications {
                    new_certified_data: None,
                    cycles_balance_change: CyclesBalanceChange::zero(),
                    reserved_cycles: Cycles::zero(),
                    consumed_cycles_by_use_case: ConsumedCyclesDuringExecution::default(),
                    call_context_balance_taken: None,
                    request_slots_used: BTreeMap::new(),
                    requests: vec![],
                    new_global_timer: None,
                    canister_log: CanisterLog::default_delta(),
                    should_bump_canister_version: false,
                },
                None => SystemStateModifications {
                    new_certified_data: None,
                    cycles_balance_change: CyclesBalanceChange::zero(),
                    reserved_cycles: Cycles::zero(),
                    consumed_cycles_by_use_case: ConsumedCyclesDuringExecution::default(),
                    call_context_balance_taken: None,
                    request_slots_used: system_state_modifications.request_slots_used,
                    requests: system_state_modifications.requests,
                    new_global_timer: None,
                    canister_log: CanisterLog::default_delta(),
                    should_bump_canister_version: false,
                },
            },
            // Replicated queries return changes to the logs and cycles balance,
            // as well as bumping the canister's version in case there was no trap.
            // In case of a trap, only changes to logs should be returned.
            ApiType::ReplicatedQuery { time, .. } => match &self.execution_error {
                Some(err) => {
                    self.add_canister_log_for_trap(err, time, &mut system_state_modifications);
                    SystemStateModifications {
                        new_certified_data: None,
                        cycles_balance_change: CyclesBalanceChange::zero(),
                        reserved_cycles: Cycles::zero(),
                        consumed_cycles_by_use_case: ConsumedCyclesDuringExecution::default(),
                        call_context_balance_taken: None,
                        request_slots_used: BTreeMap::new(),
                        requests: vec![],
                        new_global_timer: None,
                        canister_log: system_state_modifications.canister_log,
                        should_bump_canister_version: false,
                    }
                }
                None => SystemStateModifications {
                    new_certified_data: None,
                    cycles_balance_change: system_state_modifications.cycles_balance_change,
                    reserved_cycles: Cycles::zero(),
                    consumed_cycles_by_use_case: system_state_modifications
                        .consumed_cycles_by_use_case,
                    call_context_balance_taken: system_state_modifications
                        .call_context_balance_taken,
                    request_slots_used: BTreeMap::new(),
                    requests: vec![],
                    new_global_timer: None,
                    canister_log: system_state_modifications.canister_log,
                    should_bump_canister_version: true,
                },
            },
            // Replicated executions (except queries), should return all changes and bump
            // the canister version in case there was no trap. Otherwise, only changes
            // to logs are returned.
            ApiType::SystemTask { time, .. }
            | ApiType::Update { time, .. }
            | ApiType::Cleanup { time, .. }
            | ApiType::ReplyCallback { time, .. }
            | ApiType::RejectCallback { time, .. } => match &self.execution_error {
                Some(err) => {
                    self.add_canister_log_for_trap(err, time, &mut system_state_modifications);
                    SystemStateModifications {
                        new_certified_data: None,
                        cycles_balance_change: CyclesBalanceChange::zero(),
                        reserved_cycles: Cycles::zero(),
                        consumed_cycles_by_use_case: ConsumedCyclesDuringExecution::default(),
                        call_context_balance_taken: None,
                        request_slots_used: BTreeMap::new(),
                        requests: vec![],
                        new_global_timer: None,
                        canister_log: system_state_modifications.canister_log,
                        should_bump_canister_version: false,
                    }
                }
                None => {
                    system_state_modifications.should_bump_canister_version = true;
                    system_state_modifications
                }
            },
```
