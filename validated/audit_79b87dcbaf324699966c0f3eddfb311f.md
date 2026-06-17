### Title
Caller Nonce Permanently Incremented on CREATE Collision Failure — (`evm_interpreter/src/ee_trait_impl.rs`)

### Summary

In `before_executing_frame`, the caller's nonce is incremented **before** the address-collision check. When a collision is detected, the function returns `Ok(false)` without rolling back the nonce. Because `before_executing_frame` is explicitly designed to be non-revertible, the nonce increment is permanent even though the CREATE failed.

### Finding Description

In `before_executing_frame` at depth > 0 for a Constructor call, the execution order is:

1. **Nonce increment** (lines 391–413): `system.io.increment_nonce(...)` is called unconditionally for the caller.
2. **Collision check** (lines 429–443): if `deployee_code_len != 0 || deployee_nonce != 0`, all gas is burned and `Ok(false)` is returned. [1](#0-0) [2](#0-1) 

The trait contract at `zk_ee/src/system/execution_environment/mod.rs` line 67 explicitly states: *"Pre-checks and operations that should not be rolled back if actual frame execution fails."* [3](#0-2) 

The same intent is echoed in the `SupportedEEVMState` wrapper: [4](#0-3) 

There is no snapshot/rollback taken around `before_executing_frame` in the runner — the rollback handle used in `call_execute_callee_frame` only covers the actual frame execution, not the pre-frame operations: [5](#0-4) 

### Impact Explanation

Any unprivileged EVM contract can permanently increment its own nonce by issuing a CREATE/CREATE2 targeting an address that already has non-zero nonce or code. The nonce increment survives even though the deployment fails. This breaks the EVM invariant (EIP-161) that a failed CREATE due to collision must not alter the caller's nonce. Consequences include:

- Nonce values can be skipped, invalidating pre-signed transactions or counterfactual deployments that depend on a specific nonce.
- A contract can inflate its nonce arbitrarily (one increment per collision attempt), disrupting any protocol logic that relies on nonce-based address prediction.

### Likelihood Explanation

The attack requires only a deployed contract and knowledge of any address with existing code or non-zero nonce (trivially satisfiable on any live network). No privileged access, governance, or external oracle is needed. The call path is: EOA → contract A → `CREATE` targeting occupied address → nonce incremented, CREATE fails, nonce not rolled back.

### Recommendation

Move the collision check **before** the nonce increment inside `before_executing_frame`:

```rust
// 1. Check collision FIRST
if deployee_code_len != 0 || deployee_nonce != 0 {
    // burn gas, return Ok(false)
}

// 2. Only then increment nonce
if callstack_depth > 0 {
    system.io.increment_nonce(...);
}
```

This matches the EVM specification: the nonce increment is part of initiating a successful deployment, not a pre-condition that survives a collision abort. [6](#0-5) 

### Proof of Concept

State test (pseudo-EVM state test format):

1. Pre-state: deploy contract `A` at address `0xA000`; deploy any contract at address `0xB000` (non-zero code/nonce).
2. `A`'s bytecode: execute `CREATE` with salt/nonce that resolves to `0xB000`.
3. Assert: after the transaction, `A`'s nonce equals its pre-state nonce (unchanged).
4. Observed: `A`'s nonce is pre-state nonce + 1, demonstrating the spurious increment.

### Citations

**File:** evm_interpreter/src/ee_trait_impl.rs (L389-447)
```rust
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

**File:** zk_ee/src/system/execution_environment/mod.rs (L66-75)
```rust
    ///
    /// Pre-checks and operations that should not be rolled back if actual frame execution fails.
    ///
    fn before_executing_frame<'a, 'i: 'ee, 'h: 'ee>(
        system: &mut System<S>,
        frame_state: &mut ExecutionEnvironmentLaunchParams<'i, S>,
        tracer: &mut impl Tracer<S>,
    ) -> Result<bool, Self::SubsystemError>
    where
        S::IO: IOSubsystemExt;
```

**File:** basic_bootloader/src/bootloader/supported_ees.rs (L99-118)
```rust
    /// Pre-checks and operations that should not be rolled back if call fails
    pub fn before_executing_frame<'a, 'i: 'ee, 'h: 'ee>(
        ee_version: ExecutionEnvironmentType,
        system: &mut System<S>,
        frame_state: &mut ExecutionEnvironmentLaunchParams<'i, S>,
        tracer: &mut impl Tracer<S>,
    ) -> Result<bool, EESubsystemError>
    where
        S::IO: IOSubsystemExt,
    {
        match ee_version {
            ExecutionEnvironmentType::EVM => {
                SystemBoundEVMInterpreter::<S>::before_executing_frame(system, frame_state, tracer)
                    .map_err(wrap_error!())
            }
            ExecutionEnvironmentType::NoEE => Err(interface_error!(
                InterfaceError::UnsupportedExecutionEnvironment
            )),
        }
    }
```

**File:** basic_bootloader/src/bootloader/runner.rs (L506-508)
```rust
                    self.system
                        .finish_global_frame(reverted.then_some(&rollback_handle))
                        .map_err(|_| internal_error!("must finish execution frame"))?;
```
