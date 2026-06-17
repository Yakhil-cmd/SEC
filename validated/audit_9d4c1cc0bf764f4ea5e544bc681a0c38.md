### Title
Missing EVM Storage-Collision Check in CREATE/CREATE2 Deployment Allows Pre-Seeding Contract Storage - (`evm_interpreter/src/ee_trait_impl.rs`)

---

### Summary

ZKsync OS omits the EVM-required check (EIP-684) that a deployment target address must have **empty storage** before a constructor is executed. The code explicitly acknowledges this gap. An unprivileged attacker can pre-seed storage slots at a deterministic CREATE2 address before a victim contract is deployed there, causing the deployed contract to start with attacker-controlled storage state — a condition that standard EVM would prevent by failing the CREATE/CREATE2 entirely.

---

### Finding Description

EIP-684 (enforced since Spurious Dragon) requires that `CREATE` and `CREATE2` fail with a collision if the target address already has non-zero storage. ZKsync OS checks only `code_len != 0 || nonce != 0` as the collision guard, and explicitly skips the storage check:

```rust
// Check there's no contract already deployed at this address.
// NB: EVM also specifies that the address should have empty storage,
// but we cannot perform such a check for now.
if deployee_code_len != 0 || deployee_nonce != 0 {
    ...
    return Ok(false); // CreateCollision
}
``` [1](#0-0) 

This is independently confirmed by the EVM state-test index, which explicitly disables the `create2collisionStorageParis` test with the annotation `"We do not check storage for collision"`: [2](#0-1) 

The `constructor_pre_checks` helper called from `before_reading_callee` also performs no storage-emptiness check — it only validates call depth, caller balance, and nonce overflow: [3](#0-2) 

---

### Impact Explanation

**Vulnerability class:** EVM semantic mismatch / data validation — missing deployment pre-condition check.

An attacker who can predict a CREATE2 address (trivially possible: address = `keccak256(0xff ++ deployer ++ salt ++ keccak256(initcode))[12:]`) can write arbitrary values to storage slots at that address before the victim deploys there. After deployment:

- The contract's storage is not empty as its constructor assumes.
- Initialization guards that rely on a zero-valued slot (e.g., `initialized == 0`, `owner == address(0)`) are bypassed or corrupted.
- On standard EVM the CREATE2 would have **failed** at this point, protecting the deployer. On ZKsync OS it **succeeds**, silently placing the contract into an invalid state.

Concrete impact path: a proxy or upgradeable contract deployed via CREATE2 with a known salt has its `_initialized` slot (slot 0 in OpenZeppelin's `Initializable`) pre-set to `1` by the attacker. The contract deploys successfully but its `initialize()` function reverts on every call, permanently bricking the contract and any funds sent to it.

---

### Likelihood Explanation

**Medium.** The attacker must:
1. Know the CREATE2 deployer address, salt, and initcode in advance — all of which are typically public (mempool, open-source deployments, deterministic factory patterns).
2. Submit a transaction that writes to the target address before the deployment transaction is mined — a standard front-running scenario on a sequencer-based L2.

No privileged access, leaked keys, or oracle manipulation is required. The attack is fully executable by an unprivileged EOA.

---

### Recommendation

Implement the EIP-684 storage-emptiness check before executing a constructor frame. Because ZKsync OS uses a flat storage model, the check must query whether any storage slot under the target address is non-zero. If the check cannot be made efficient enough for the current storage model, the known divergence must be documented as an accepted risk in the security model and the Immunefi scope, and deployers must be warned not to use predictable CREATE2 salts for security-sensitive contracts on ZKsync OS.

---

### Proof of Concept

**Step 1 — Attacker pre-seeds storage.**
Deploy a contract `Poisoner` that calls `SSTORE(0, attacker_address)` at the known CREATE2 target address. Because ZKsync OS does not enforce storage-emptiness on deployment, this write persists.

**Step 2 — Victim deploys.**
The victim protocol deploys `MyProxy` via CREATE2 with the same (deployer, salt, initcode) tuple. `before_executing_frame` checks only `code_len` and `nonce` (both zero), so the constructor runs.

**Step 3 — Corrupted initial state.**
Inside `MyProxy`'s constructor, `slot[0]` already equals `attacker_address`. Any guard of the form `require(owner == address(0))` or `require(!initialized)` reverts, or the attacker is silently installed as owner.

**Relevant code path:**

```
evm_interpreter::Interpreter::create_immediate_return_state   // constructor exit
  → system.deploy_bytecode(...)                               // code written
    ← before_executing_frame checks code_len/nonce only      // storage NOT checked
``` [4](#0-3) [5](#0-4)

### Citations

**File:** evm_interpreter/src/ee_trait_impl.rs (L389-443)
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
```

**File:** evm_interpreter/src/ee_trait_impl.rs (L489-523)
```rust
/// Checks that must pass before a CREATE/CREATE2 callee is read:
/// depth, balance, and nonce overflow (read-only check).
fn constructor_pre_checks<S: EthereumLikeTypes>(
    system: &mut System<S>,
    call_request: &mut ExternalCallRequest<S>,
    callstack_depth: usize,
) -> Result<Option<EvmError>, SubsystemError<EvmErrors>>
where
    S::IO: IOSubsystemExt,
{
    if let Some(error) = check_depth_and_balance(system, call_request, callstack_depth)? {
        return Ok(Some(error));
    }

    // Read-only nonce overflow check (actual increment happens in before_executing_frame)
    if callstack_depth > 0 {
        let caller_nonce = call_request
            .available_resources
            .with_infinite_ergs(|inf_resources| {
                system.io.read_account_properties(
                    THIS_EE_TYPE,
                    inf_resources,
                    &call_request.caller,
                    AccountDataRequest::empty().with_nonce(),
                )
            })?
            .nonce
            .0;
        if caller_nonce == u64::MAX {
            return Ok(Some(EvmError::NonceOverflow));
        }
    }

    Ok(None)
}
```

**File:** tests/evm_tester/indexes/develop-state-tests.yaml (L2367-2370)
```yaml
              create2collisionStorageParis.json:
                hash: '0x1c8ea0d96bc40fd2a8997e58950d6bdb'
                enabled: false
                comment: We do not check storage for collision
```

**File:** evm_interpreter/src/interpreter.rs (L366-409)
```rust
        let result = if self.is_constructor {
            let deployed_code = return_values.returndata;
            let mut error_after_constructor = None;
            if deployed_code.len() > MAX_CODE_SIZE {
                // EIP-170: reject code of length > 24576.
                error_after_constructor = Some(EvmError::CreateContractSizeLimit)
            } else if !deployed_code.is_empty() && deployed_code[0] == 0xEF {
                // EIP-3541: reject code starting with 0xEF.
                error_after_constructor = Some(EvmError::CreateContractStartingWithEF);
            } else {
                match system.deploy_bytecode(
                    THIS_EE_TYPE,
                    self.gas.resources_mut(),
                    &self.address,
                    deployed_code,
                ) {
                    Ok((
                        actual_deployed_bytecode,
                        internal_bytecode_hash,
                        observable_bytecode_len,
                    )) => {
                        system_log!(
                            system,
                            "Successfully deployed contract at {:?} \n",
                            self.address
                        );

                        tracer.on_bytecode_change(
                            THIS_EE_TYPE,
                            self.address,
                            Some(actual_deployed_bytecode),
                            internal_bytecode_hash,
                            observable_bytecode_len,
                        );
                    }
                    Err(SystemError::LeafRuntime(RuntimeError::OutOfErgs(_))) => {
                        error_after_constructor = Some(EvmError::CodeStoreOutOfGas);
                    }
                    Err(SystemError::LeafRuntime(RuntimeError::FatalRuntimeError(e))) => {
                        return Err(RuntimeError::FatalRuntimeError(e).into())
                    }
                    Err(SystemError::LeafDefect(e)) => return Err(e.into()),
                }
            }
```
