### Title
Incomplete CREATE Collision Check Allows Attacker to Pre-Populate Storage at Predicted Deployment Address - (`evm_interpreter/src/ee_trait_impl.rs`)

### Summary

ZKsync OS's EVM interpreter explicitly omits the EIP-684 storage-emptiness check during `CREATE`/`CREATE2` deployment collision detection. An unprivileged attacker can pre-populate storage slots at a deterministically predicted `CREATE` deployment address before the victim deploys, causing the constructor to execute with attacker-controlled storage state. This is a direct analog to the original report's vulnerability class: nonce-based address derivation creates predictable deployment targets that can be manipulated to steal funds or corrupt contract initialization.

---

### Finding Description

The EVM specification (EIP-684) requires that a `CREATE` deployment **must fail** if the target address already has non-empty storage, in addition to the existing checks for non-zero code length and non-zero nonce. This prevents an attacker from pre-seeding storage at a predicted deployment address to corrupt a victim's constructor logic.

In ZKsync OS, the collision check in `before_executing_frame` only tests `code_len != 0 || nonce != 0`. The storage check is explicitly acknowledged as missing with a `// NB:` comment:

```rust
// Check there's no contract already deployed at this address.
// NB: EVM also specifies that the address should have empty storage,
// but we cannot perform such a check for now.
if deployee_code_len != 0 || deployee_nonce != 0 {
    ...
    return Ok(false);
}
``` [1](#0-0) 

The `CREATE` address is derived deterministically from `(deployer_address, deployer_nonce)` via RLP + Keccak256:

```rust
pub fn derive_address_for_deployment_create(
    _resources: &mut <S as SystemTypes>::Resources,
    deployer_address: &<S::IOTypes as SystemIOTypesConfig>::Address,
    deployer_nonce: u64,
) -> Result<...> {
    let encoding_it = crate::utils::create_quasi_rlp(deployer_address, deployer_nonce);
    ...
    let new_address = Keccak256::digest(&buffer[..encoding_len]);
``` [2](#0-1) 

This same derivation is used for top-level deployment transactions in both the Ethereum and ZK transaction flows: [3](#0-2) [4](#0-3) 

Because the deployer's nonce is publicly observable on-chain, any observer can compute the exact address at which the next `CREATE` from a given account will land.

---

### Impact Explanation

An attacker who pre-populates storage at the predicted deployment address causes the victim's constructor to execute with attacker-controlled initial storage state. Concrete impact scenarios:

1. **Initialization bypass**: A contract whose constructor checks `if (slot[0] == 0) { initialize(); }` will skip initialization if the attacker pre-set `slot[0] = 1`. The contract is deployed in a permanently broken/attacker-controlled state.
2. **Fund theft**: If the constructor mints tokens or distributes funds based on a storage-resident flag (e.g., `isInitialized`), the attacker can pre-set that flag to redirect funds or claim ownership.
3. **Proxy/ownership takeover**: Upgradeable proxy patterns that store the admin address in slot 0 during construction can be hijacked if the attacker pre-writes their own address to that slot before deployment.

The deployed contract will have a non-zero bytecode hash and nonce after construction, so subsequent collision checks will correctly block re-deployment — but the damage is already done during the first constructor execution.

---

### Likelihood Explanation

- The deployer's nonce is publicly readable from chain state before the deployment transaction is included.
- Pre-populating storage at an arbitrary address requires only a simple `SSTORE` call from any contract, which is an unprivileged operation.
- The attacker only needs to front-run the deployment transaction (or act in the same block on ZKsync's sequencer model), which is realistic given that ZKsync OS processes transactions in a sequencer-controlled order where transaction ordering is observable.
- The missing check is explicitly documented in the source code, confirming the developers are aware of the gap.

---

### Recommendation

Implement the EIP-684 storage-emptiness check before allowing constructor execution. The check must verify that the target address has no non-zero storage slots. If a full storage scan is not feasible at deployment time, the system should at minimum:

1. Track whether any storage has ever been written to an address (e.g., via a per-address "has-storage" flag in account properties).
2. Reject `CREATE` deployments to addresses where this flag is set.

Alternatively, enforce that `CREATE` deployments always use a salt-inclusive scheme (analogous to `CREATE2` with `msg.sender` in the salt) to make address prediction non-trivial for external observers.

---

### Proof of Concept

1. Alice has nonce `N` at address `0xAlice`. The next `CREATE` from Alice will deploy to `addr = keccak256(rlp([0xAlice, N]))[12:]`.
2. Bob computes `addr` off-chain by reading Alice's current nonce from the chain.
3. Bob calls a contract that executes `SSTORE(addr, slot=0, value=1)` — this writes to storage at `addr` before any code is deployed there.
4. Alice submits a deployment transaction. The constructor runs at `addr`.
5. The collision check at `evm_interpreter/src/ee_trait_impl.rs:429` passes because `code_len == 0` and `nonce == 0` at `addr`.
6. The constructor reads `slot[0]` and finds `1` (Bob's value) instead of `0`. If the constructor logic is `if slot[0] == 0 { owner = msg.sender; }`, Alice's contract is deployed without an owner, or with Bob as the effective owner.
7. Any ETH sent to `addr` during or after deployment is accessible to Bob's pre-planted logic if the constructor does not overwrite the storage. [5](#0-4)

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

**File:** evm_interpreter/src/interpreter.rs (L455-473)
```rust
    pub fn derive_address_for_deployment_create(
        _resources: &mut <S as SystemTypes>::Resources,
        deployer_address: &<S::IOTypes as SystemIOTypesConfig>::Address,
        deployer_nonce: u64,
    ) -> Result<<S::IOTypes as SystemIOTypesConfig>::Address, EvmSubsystemError> {
        use crypto::sha3::{Digest, Keccak256};
        let mut buffer = [0u8; crate::utils::MAX_CREATE_RLP_ENCODING_LEN];
        let encoding_it = crate::utils::create_quasi_rlp(deployer_address, deployer_nonce);
        let encoding_len = ExactSizeIterator::len(&encoding_it);
        for (dst, src) in buffer.iter_mut().zip(encoding_it) {
            *dst = src;
        }
        let new_address = Keccak256::digest(&buffer[..encoding_len]);
        #[allow(deprecated)]
        let new_address =
            B160::try_from_be_slice(&new_address.as_slice()[12..]).expect("must create address");

        Ok(new_address)
    }
```

**File:** basic_bootloader/src/bootloader/transaction_flow/ethereum/mod.rs (L757-772)
```rust
        let deployed_address = match to_ee_type {
            ExecutionEnvironmentType::NoEE => {
                return Err(internal_error!("Deployment cannot target NoEE").into())
            }
            ExecutionEnvironmentType::EVM => {
                SystemBoundEVMInterpreter::<S>::derive_address_for_deployment_create(
                    &mut resources,
                    &from,
                    context.originator_nonce_to_use,
                )
                .map_err(|e| {
                    let ee_error: EESubsystemError = wrap_error!(e);
                    wrap_error!(ee_error)
                })?
            }
        };
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/mod.rs (L710-725)
```rust
        let deployed_address = match to_ee_type {
            ExecutionEnvironmentType::NoEE => {
                return Err(internal_error!("Deployment cannot target NoEE").into())
            }
            ExecutionEnvironmentType::EVM => {
                SystemBoundEVMInterpreter::<S>::derive_address_for_deployment_create(
                    &mut resources,
                    &from,
                    context.originator_nonce_to_use,
                )
                .map_err(|e| {
                    let ee_error: EESubsystemError = wrap_error!(e);
                    wrap_error!(ee_error)
                })?
            }
        };
```
