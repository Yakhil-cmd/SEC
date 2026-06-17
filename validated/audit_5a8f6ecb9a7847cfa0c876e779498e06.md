Let me read the key file more carefully to verify the exact lines and understand the full context.

Now I have a complete picture. Let me confirm the exact ordering in `runner.rs` and the `start_executing_frame` context.

### Title
Caller Nonce Permanently Incremented Before CREATE Address Collision Check — (`evm_interpreter/src/ee_trait_impl.rs`)

---

### Summary

In `before_executing_frame`, the caller's nonce is incremented **before** the address-collision guard is evaluated. When a collision is detected the function returns `Ok(false)` without rolling back the nonce write, permanently corrupting the caller contract's nonce. Because `before_executing_frame` is invoked before `start_global_frame()` in the runner, the increment is outside every rollback snapshot and cannot be undone.

---

### Finding Description

`before_executing_frame` in `evm_interpreter/src/ee_trait_impl.rs` performs two sequential operations for nested `Constructor` calls (`callstack_depth > 0`):

**Step 1 — nonce increment (lines 391–413):**
```rust
if frame_state.environment_parameters.callstack_depth > 0 {
    system.io.increment_nonce(
        THIS_EE_TYPE, inf_resources,
        &frame_state.external_call.caller,   // ← caller's nonce
        1u64,
    )
    ...
}
``` [1](#0-0) 

**Step 2 — collision check (lines 429–443):**
```rust
if deployee_code_len != 0 || deployee_nonce != 0 {
    // burn all gas, emit CreateCollision
    return Ok(false);   // ← returns WITHOUT rolling back the nonce
}
``` [2](#0-1) 

The entire `before_executing_frame` call is made **before** the rollback snapshot is opened:

```rust
// Pre-checks and operations that should not be rolled back if call fails
match SupportedEEVMState::before_executing_frame(...) { ... }

// Create snapshot for rollbacks
let rollback_handle = self.system.start_global_frame()?;
``` [3](#0-2) 

Consequently, when `Ok(false)` is returned due to a collision, the caller's nonce has already been written to the cache and is **not** covered by any rollback handle. The deployee's EIP-161 nonce-to-1 write, by contrast, happens inside `start_executing_frame` (line 155) which runs after the snapshot is taken and is correctly rolled back on failure. [4](#0-3) 

This is structurally identical to the reported pattern: a state variable (`executionPrice`) is updated before a potentially-failing call (`poolUpkeep`), and if the call fails the state is left in an incorrect intermediate value.

---

### Impact Explanation

**EVM semantic mismatch / state-transition bug.** In canonical EVM, a CREATE that fails due to address collision produces no net state change for the caller. Here the caller's nonce is permanently incremented by 1 even though the deployment never occurred. This has two concrete consequences:

1. **Deterministic address derivation is broken.** All subsequent `CREATE` calls from the same contract will derive addresses from a nonce that is one higher than the EVM-correct value, placing new contracts at unexpected addresses.
2. **Forward/proving divergence.** The forward runner and the prover both execute the same bootloader logic, so both will record the spurious nonce increment. Any external system (e.g., a wallet, a factory contract, a bridge) that independently computes the expected CREATE address using the canonical EVM nonce will disagree with the on-chain state.

---

### Likelihood Explanation

The trigger is fully attacker-controlled and requires no privileged access:

1. The attacker observes (or predicts) the nonce of a target factory contract.
2. The attacker computes `keccak256(rlp([factory_address, nonce]))` to obtain the next CREATE address.
3. The attacker deploys any contract to that address first (setting `deployee_nonce = 1`).
4. The next time the factory calls `CREATE`, `before_executing_frame` increments the factory's nonce and then detects the collision, returning `Ok(false)` with the nonce already written.

This is a single-transaction, permissionless attack. Factory contracts that rely on deterministic CREATE addresses (e.g., minimal-proxy deployers, counterfactual wallet factories) are directly affected.

---

### Recommendation

Move the collision check **before** the nonce increment inside `before_executing_frame`, or restructure so the nonce is only incremented after all pre-flight checks pass:

```rust
// 1. Check collision FIRST
if deployee_code_len != 0 || deployee_nonce != 0 {
    tracer.evm_tracer().on_call_error(&EvmError::CreateCollision);
    return Ok(false);
}

// 2. Only then increment caller nonce
if frame_state.environment_parameters.callstack_depth > 0 {
    system.io.increment_nonce(..., &frame_state.external_call.caller, 1u64)?;
}
```

This mirrors the fix recommended in the original report: update state only after the potentially-failing operation has succeeded.

---

### Proof of Concept

```
Block N:
  Attacker tx: deploy contract C_collision at address A
    where A = keccak256(rlp([factory_addr, factory_nonce]))

Block N (or later):
  Any tx that causes factory_contract to call CREATE:
    → before_executing_frame is entered
    → factory_contract.nonce is incremented: factory_nonce → factory_nonce + 1
    → collision check: C_collision.nonce == 1 → Ok(false) returned
    → factory_contract.nonce is now permanently factory_nonce + 1

Next factory CREATE call:
    → derives address from factory_nonce + 1 instead of factory_nonce
    → contract lands at wrong address, breaking all off-chain address predictions
``` [5](#0-4) [3](#0-2)

### Citations

**File:** evm_interpreter/src/ee_trait_impl.rs (L146-164)
```rust
            CallModifier::Constructor => {
                // EIP-161: contracts should be initialized with nonce 1
                // Note: this has to be done before we actually deploy the bytecode,
                // as constructor execution should see the deployed_address as having
                // nonce = 1
                available_resources
                    .with_infinite_ergs(|inf_resources| {
                        system
                            .io
                            .increment_nonce(THIS_EE_TYPE, inf_resources, &this_address, 1)
                    })
                    .map_err(|e| -> EvmSubsystemError {
                        match e {
                            SubsystemError::LeafRuntime(RuntimeError::FatalRuntimeError(_)) => {
                                wrap_error!(e)
                            }
                            _ => internal_error!("Failed to set deployed nonce to 1").into(),
                        }
                    })?;
```

**File:** evm_interpreter/src/ee_trait_impl.rs (L372-447)
```rust
    fn before_executing_frame<'a, 'i: 'ee, 'h: 'ee>(
        system: &mut System<S>,
        frame_state: &mut ExecutionEnvironmentLaunchParams<'i, S>,
        tracer: &mut impl Tracer<S>,
    ) -> Result<bool, Self::SubsystemError>
    where
        S::IO: IOSubsystemExt,
    {
        if let Some(error) = check_depth_and_balance(
            system,
            &mut frame_state.external_call,
            frame_state.environment_parameters.callstack_depth,
        )? {
            tracer.evm_tracer().on_call_error(&error);
            return Ok(false);
        }

        if frame_state.external_call.modifier == CallModifier::Constructor {
            // Increase nonce. Ignore, if we are in the root frame - caller's nonce already incremented before.
            if frame_state.environment_parameters.callstack_depth > 0 {
                match frame_state
                    .external_call
                    .available_resources
                    .with_infinite_ergs(|inf_resources| {
                        system.io.increment_nonce(
                            THIS_EE_TYPE,
                            inf_resources,
                            &frame_state.external_call.caller,
                            1u64,
                        )
                    }) {
                    Ok(_) => {}
                    Err(SubsystemError::LeafUsage(InterfaceError(
                        NonceError::NonceOverflow,
                        _,
                    ))) => {
                        tracer.evm_tracer().on_call_error(&EvmError::NonceOverflow);
                        return Ok(false);
                    }
                    Err(e) => return Err(wrap_error!(e)),
                };
            };

            let deployee_code_len = frame_state
                .environment_parameters
                .callee_account_properties
                .unpadded_code_len;
            let deployee_nonce = frame_state
                .environment_parameters
                .callee_account_properties
                .nonce;

            // Check there's no contract already deployed at this address.
            // NB: EVM also specifies that the address should have empty storage,
            // but we cannot perform such a check for now.
            // We need to check this here (not when we actually deploy the code)
            // because if this check fails the constructor shouldn't be executed.
            if deployee_code_len != 0 || deployee_nonce != 0 {
                system_log!(system, "Deployment on existing account\n",);
                frame_state
                    .external_call
                    .available_resources
                    .charge(&S::Resources::from_ergs(
                        frame_state.external_call.available_resources.ergs(),
                    ))
                    .expect("Should succeed"); // Burn all gas

                tracer
                    .evm_tracer()
                    .on_call_error(&EvmError::CreateCollision);
                return Ok(false);
            }
        }

        Ok(true)
    }
```

**File:** basic_bootloader/src/bootloader/runner.rs (L253-276)
```rust
        // Pre-checks and operations that should not be rolled back if call fails
        match SupportedEEVMState::before_executing_frame(
            interpret_as_ee_type,
            self.system,
            &mut external_call_launch_params,
            tracer,
        ) {
            Ok(success) => {
                if !success {
                    return Ok((
                        external_call_launch_params
                            .external_call
                            .available_resources,
                        CallResult::Failed {
                            return_values: ReturnValues::empty(),
                        },
                    ));
                }
            }
            Err(e) => return Err(wrap_error!(e)),
        }

        // Create snapshot for rollbacks
        let rollback_handle = self.system.start_global_frame()?;
```
