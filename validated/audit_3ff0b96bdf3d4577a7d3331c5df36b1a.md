### Title
Missing Storage Collision Check in CREATE/CREATE2 Allows Deployment to Succeed on Dirty Address - (File: `evm_interpreter/src/ee_trait_impl.rs`)

### Summary
ZKsync OS's EVM interpreter omits the EIP-161/Paris-era storage-emptiness check from its CREATE/CREATE2 collision guard. When a target address has non-zero storage but zero nonce and empty code, Ethereum rejects the deployment with a collision error; ZKsync OS silently proceeds, deploying the new contract on top of stale storage. An unprivileged attacker can exploit this to poison the storage of a deterministically-addressed contract before it is deployed, causing the new contract to observe attacker-controlled initial state.

### Finding Description

The EVM specification (EIP-161, enforced since Spurious Dragon and still active under Cancun) requires that a CREATE or CREATE2 deployment fail if the target address satisfies **any** of: non-zero nonce, non-empty code, or non-empty storage. ZKsync OS only enforces the first two conditions.

In `before_executing_frame` inside `evm_interpreter/src/ee_trait_impl.rs`:

```rust
// Check there's no contract already deployed at this address.
// NB: EVM also specifies that the address should have empty storage,
// but we cannot perform such a check for now.
if deployee_code_len != 0 || deployee_nonce != 0 {
    // … burn gas, emit CreateCollision, return Ok(false)
}
```

The comment explicitly acknowledges the missing third condition. [1](#0-0) 

This divergence is also recorded in the official documentation:

> "Deployment doesn't fail if the storage for the deployed address is already used (when nonce is 0 and code is empty)." [2](#0-1) 

The Ethereum test suite cases that exercise this exact path are explicitly disabled in all three test-index files with the comment `"We do not check storage for collision"` / `"We do not check for storage collisions"`: [3](#0-2) [4](#0-3) 

### Impact Explanation

**EVM semantic mismatch / stale-storage inheritance.** An attacker who can write storage to a known CREATE2 target address before the legitimate deployer arrives can cause the newly deployed contract to start with attacker-controlled storage values. Contracts that rely on storage slots being zero-initialised at construction time (e.g., proxy initialisation guards, ownership flags, balance accumulators) will silently inherit the poisoned values. Depending on the contract logic, this can lead to:

- Bypassing `initializer` / `onlyOnce` guards (ownership taken by attacker).
- Corrupting accounting state (balances, allowances) from the first block.
- Defeating upgrade-proxy patterns that check `_initialized` storage slots.

The ZKsync OS state transition function will produce a different post-state root than a canonical Ethereum node would for the same transaction sequence, constituting a provable forward/proving divergence.

### Likelihood Explanation

**Moderate.** The precondition — an address with non-empty storage but zero nonce and empty code — is achievable under Cancun via the same-transaction SELFDESTRUCT path (EIP-6780): a contract deployed and selfdestructed in the same transaction has its code and nonce cleared, but if ZKsync OS's storage-clearing path (`clear_state_impl`) is not reached for every storage slot written during that constructor frame, residual storage persists. The attacker controls the initcode, the salt, and the timing, making the setup fully permissionless. The `create2collisionStorageParis.json` and `RevertInCreateInInitCreate2Paris.json` test cases being disabled confirm the condition is reachable in the test harness.

### Recommendation

Add a storage-emptiness check to the collision guard in `before_executing_frame`. Because ZKsync OS uses a flat storage model that does not maintain a per-account storage root in the hot path, the check must be performed by querying whether any storage slot for the target address is non-zero before allowing the constructor to execute. If a full storage scan is too expensive, a per-account "has-ever-had-storage" flag written at first `SSTORE` and cleared on full deconstruction would suffice.

### Proof of Concept

```
Block 1, Tx 1 (attacker):
  Deploy contract C at address X via CREATE2(salt=S, initcode=I)
  Constructor of C:
    PUSH1 0x01
    PUSH1 0x00
    SSTORE          ; storage[0] = 1  (poison slot)
    PUSH20 <attacker>
    SELFDESTRUCT    ; EIP-6780 same-tx: clears code+nonce+balance
                    ; ZKsync OS: storage[0] = 1 may persist if
                    ;            clear_state_impl is not invoked

Block 1, Tx 2 (victim protocol):
  Deploy contract V at address X via CREATE2(salt=S, initcode=I')
  ZKsync OS: deployee_code_len == 0, deployee_nonce == 0
             → collision check passes → constructor runs
             → V.storage[0] == 1  (attacker-controlled)
  Ethereum:  storage[0] != 0 → CreateCollision → deployment reverts

Result: ZKsync OS post-state diverges from Ethereum canonical state.
        Contract V starts with poisoned storage[0] = 1.
```

The attacker entry path is a standard EVM transaction requiring no privileged role. The `CREATE2` address derivation in `derive_address_for_deployment_create2` is deterministic and public, so the target address is predictable by any observer. [5](#0-4)

### Citations

**File:** evm_interpreter/src/ee_trait_impl.rs (L424-443)
```rust
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

**File:** docs/execution_environments/evm.md (L11-11)
```markdown
- Deployment doesn’t fail if the storage for the deployed address is already used (when nonce is 0 and code is empty).
```

**File:** tests/evm_tester/indexes/develop-blockchain-tests.yaml (L2737-2740)
```yaml
              RevertInCreateInInitCreate2Paris.json:
                hash: '0xd89c5a3c6ccc430e2e4a0c6bb5d2dd01'
                enabled: false
                comment: We do not check for storage collisions
```

**File:** tests/evm_tester/indexes/develop-blockchain-tests.yaml (L2789-2792)
```yaml
              create2collisionStorageParis.json:
                hash: '0x575599861f8c1a6319cf46a3cc8bc52c'
                enabled: false
                comment: We do not check storage for collision
```

**File:** evm_interpreter/src/interpreter.rs (L475-518)
```rust
    pub fn derive_address_for_deployment_create2(
        system: &mut System<S>,
        resources: &mut <S as SystemTypes>::Resources,
        salt: &U256,
        deployer_address: &<S::IOTypes as SystemIOTypesConfig>::Address,
        deployment_code: &[u8],
    ) -> Result<<S::IOTypes as SystemIOTypesConfig>::Address, EvmSubsystemError> {
        use crypto::sha3::{Digest, Keccak256};
        // we need to compute address based on the hash of the code and salt
        let mut initcode_hash = ArrayBuilder::default();
        resources
            .with_infinite_ergs(|inf_resources| {
                S::SystemFunctions::keccak256(
                    deployment_code,
                    &mut initcode_hash,
                    inf_resources,
                    system.get_allocator(),
                )
            })
            .map_err(|e| -> EvmSubsystemError {
                match e.root_cause() {
                    RootCause::Runtime(e @ RuntimeError::FatalRuntimeError(_)) => {
                        e.clone_or_copy().into()
                    }
                    _ => internal_error!("Keccak in create2 cannot fail").into(),
                }
            })?;
        let initcode_hash = Bytes32::from_array(initcode_hash.build());

        let mut create2_buffer = [0xffu8; 1 + 20 + 32 + 32];
        create2_buffer[1..(1 + 20)]
            .copy_from_slice(&deployer_address.to_be_bytes::<{ B160::BYTES }>());
        create2_buffer[(1 + 20)..(1 + 20 + 32)]
            .copy_from_slice(&salt.to_be_bytes::<{ U256::BYTES }>());
        create2_buffer[(1 + 20 + 32)..(1 + 20 + 32 + 32)]
            .copy_from_slice(initcode_hash.as_u8_array_ref());

        let new_address = Keccak256::digest(&create2_buffer);
        #[allow(deprecated)]
        let new_address =
            B160::try_from_be_slice(&new_address.as_slice()[12..]).expect("must create address");

        Ok(new_address)
    }
```
