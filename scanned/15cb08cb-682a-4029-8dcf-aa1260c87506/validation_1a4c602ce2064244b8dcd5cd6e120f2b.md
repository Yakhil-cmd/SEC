I have now traced the full code path. Let me summarize the findings.

**Full call chain:**

`execution_finished` (controller_service_impl.rs) → completion closure → `process_completion` → `update_execution_state` → returns `CanisterStateChanges { system_state_modifications }` → `apply_canister_state_changes` → `system_state_modifications.apply_changes()` → `validate_cycle_change` + `push_message`

**Key findings from the code:**

1. `validate_cycle_change` only checks that cycles are not *created* — it verifies `cycles_balance_change == sum(req.payment) + fees`. It does NOT validate the `receiver` or that requests were legitimately initiated. [1](#0-0) 

2. `push_message` pushes the request to the output queue with no validation of the receiver or payment amount. [2](#0-1) 

3. `push_output_request` on `SystemState` only asserts `request.sender == self.canister_id` — the sandbox knows the canister ID and can set this correctly. [3](#0-2) 

4. The code explicitly acknowledges sandbox compromise as a threat for instruction limits, but applies no analogous guard to `requests`: [4](#0-3) 

5. `update_execution_state` passes `system_state_modifications` through to the caller with no filtering of the `requests` field: [5](#0-4) 

---

### Title
Compromised Sandbox Process Can Inject Arbitrary Inter-Canister Requests with Forged Receivers and Cycle Payments — (`rs/embedders/src/wasmtime_embedder/system_api/sandbox_safe_system_state.rs`)

### Summary
The replica's `apply_changes` function validates that cycles are not *created* (`validate_cycle_change`) but does not validate that `OutputRequest` entries in `SystemStateModifications.requests` were legitimately initiated by the canister's Wasm code. A compromised sandbox process can inject requests with arbitrary `receiver` canister IDs and non-zero `payment` cycles, as long as `cycles_balance_change` is set consistently — which the sandbox fully controls.

### Finding Description
`SystemStateModifications` is a serializable struct transmitted over IPC from the sandbox process to the replica. The `requests: Vec<OutputRequest>` field is populated by the sandbox and accepted verbatim by the replica. In `apply_changes`:

1. `validate_cycle_change` checks only that `cycles_balance_change == Σ(req.payment) + fees`. A compromised sandbox can set both `requests[i].payment = X` and `cycles_balance_change = Removed(X)` simultaneously, passing this check trivially.
2. `push_message` then calls `system_state.push_output_request(msg, time)` with no validation of `msg.receiver` or `msg.payment`.
3. `push_output_request` in `SystemState` only asserts `request.sender == self.canister_id` — the sandbox knows the canister ID and sets it correctly.

The replica explicitly guards against sandbox-inflated instruction counts (`process_completion` lines 1713–1724) but applies no equivalent guard to injected output requests. [6](#0-5) 

### Impact Explanation
A compromised sandbox process can cause the victim canister to send inter-canister calls with arbitrary cycle payments to attacker-controlled canisters. This constitutes unauthorized cycles exfiltration. The canister's balance is debited by the injected payment amount, and the attacker's canister receives the cycles when the message is processed. The only precondition is that the victim canister has a sufficient cycles balance.

### Likelihood Explanation
Exploiting this requires first compromising the sandbox process (e.g., via a Wasm runtime escape in wasmtime, a memory-safety bug in the embedder, or a JIT-spray attack enabled by `mprotect(PROT_EXEC)` on anonymous pages — explicitly noted as a risk in the SELinux policy docs). The IC's security model explicitly treats sandbox compromise as a threat to defend against, and the instruction-limit guard demonstrates this intent. The missing guard on `requests` is an oversight in that defense-in-depth model. [7](#0-6) 

### Recommendation
In `apply_changes`, before calling `push_message`, validate each `OutputRequest` against the set of requests that were legitimately registered via `SandboxSafeSystemState::push_output_request` during Wasm execution. One approach: record a cryptographic commitment (e.g., a hash or count) of the requests inside the trusted `SandboxSafeSystemState` before serialization, and verify it on the replica side. Alternatively, re-derive the expected request list from the `SandboxSafeSystemState` snapshot sent to the sandbox at execution start, and reject any `OutputRequest` not matching a legitimately initiated call.

### Proof of Concept
```rust
// In a test, directly construct a SystemStateModifications with a forged request:
let forged_payment = Cycles::new(1_000_000_000_000);
let forged_request = OutputRequest {
    sender: victim_canister_id,       // sandbox knows this
    receiver: attacker_canister_id,   // arbitrary
    payment: forged_payment,
    // ... other fields set to valid defaults
};
let modifications = SystemStateModifications {
    requests: vec![forged_request],
    cycles_balance_change: CyclesBalanceChange::Removed(forged_payment), // consistent
    consumed_cycles_by_use_case: Default::default(),
    ..Default::default()
};
// validate_cycle_change passes: expected == Removed(forged_payment) == actual
modifications.apply_changes(time, &mut system_state, &topology, subnet_id, false, &metrics, &log).unwrap();
// Assert: system_state output queue now contains a request to attacker_canister_id with forged_payment cycles
assert!(system_state.output_queue_iter().any(|(_, msg)| msg.receiver == attacker_canister_id));
``` [8](#0-7) [9](#0-8)

### Citations

**File:** rs/embedders/src/wasmtime_embedder/system_api/sandbox_safe_system_state.rs (L157-191)
```rust
impl SystemStateModifications {
    /// Checks that no cycles were created during the execution of this message
    /// (unless the canister is the cycles minting canister).
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

**File:** rs/embedders/src/wasmtime_embedder/system_api/sandbox_safe_system_state.rs (L268-289)
```rust
    fn push_message(
        system_state: &mut SystemState,
        time: Time,
        msg: OutputRequest,
        logger: &ReplicaLogger,
    ) -> HypervisorResult<()> {
        let sent_cycles = msg.payment.get();
        let msg_receiver = msg.receiver;
        system_state
            .push_output_request(msg, time)
            .map_err(|e| Self::error(format!("Failed to push output request: {e:?}")))?;
        if sent_cycles > LOG_CANISTER_OPERATION_CYCLES_THRESHOLD {
            info!(
                logger,
                "Canister {} sent {} cycles to canister {}.",
                system_state.canister_id(),
                sent_cycles,
                msg_receiver
            );
        }
        Ok(())
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

**File:** rs/canister_sandbox/src/replica_controller/sandboxed_execution_controller.rs (L1713-1724)
```rust
        // If sandbox is compromised this value could be larger than the initial limit.
        if exec_output.wasm.num_instructions_left > message_instruction_limit {
            exec_output.wasm.num_instructions_left = message_instruction_limit;
            self.metrics
                .sandboxed_execution_instructions_left_error
                .inc();
            error!(
                self.logger,
                "[EXC-BUG] Canister {} completed execution with more instructions left than the initial limit.",
                canister_id
            )
        }
```

**File:** rs/canister_sandbox/src/replica_controller/sandboxed_execution_controller.rs (L1761-1770)
```rust
        let StateModifications {
            execution_state_modifications,
            system_state_modifications,
        } = exec_output.take_state_modifications();

        match execution_state_modifications {
            None => CanisterStateChanges {
                execution_state_changes: None,
                system_state_modifications,
            },
```
