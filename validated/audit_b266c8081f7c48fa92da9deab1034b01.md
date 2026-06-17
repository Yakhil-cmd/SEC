### Title
Missing Storage Collision Check in CREATE/CREATE2 Deployment Allows Deployment to Addresses with Pre-existing Storage - (File: `evm_interpreter/src/ee_trait_impl.rs`)

---

### Summary

ZKsync OS's EVM interpreter explicitly skips the EVM-required storage-emptiness check during CREATE/CREATE2 collision detection. Per EIP-7610 (active from Prague/Osaka), a deployment must fail if the target address has non-zero storage, even if its code and nonce are both zero. ZKsync OS only checks code length and nonce, silently omitting the storage check. This is the direct analog of the reported pattern: a function that claims to perform a complete safety check but silently drops one of its required sub-checks.

---

### Finding Description

In `evm_interpreter/src/ee_trait_impl.rs`, the `before_executing_frame` function performs the CREATE/CREATE2 collision check:

```rust
// Check there's no contract already deployed at this address.
// NB: EVM also specifies that the address should have empty storage,
// but we cannot perform such a check for now.
if deployee_code_len != 0 || deployee_nonce != 0 {
    // ... burn gas, return collision error
}
``` [1](#0-0) 

The comment explicitly acknowledges that the EVM specification also requires the target address to have empty storage, but this check is not implemented. The collision guard therefore only rejects deployments where `code_len != 0 || nonce != 0`, allowing deployment to proceed when an address has zero code and zero nonce but non-zero storage slots.

This is confirmed by multiple disabled test entries across the test index files: [2](#0-1) [3](#0-2) [4](#0-3) 

The `BasicBootloaderExecutionConfig` trait governs execution modes; none of the configs (`BasicBootloaderProvingExecutionConfig`, `BasicBootloaderForwardSimulationConfig`, `BasicBootloaderForwardETHLikeConfig`) add the missing storage check. [5](#0-4) 

---

### Impact Explanation

**Scenario:** A contract was previously deployed at a deterministic CREATE2 address and then selfdestructed. In EVM, SELFDESTRUCT clears code and resets nonce to 0, but leaves storage intact. Per EIP-7610, a subsequent CREATE2 to that address must fail because the address has non-zero storage. ZKsync OS allows the deployment to succeed.

The newly deployed contract starts with attacker-pre-populated storage slots. Depending on the contract's logic, this can:
- Bypass `initialized` / `_initialized` flags (e.g., OpenZeppelin `Initializable` pattern), allowing re-initialization
- Pre-set ownership or access-control storage slots to attacker-controlled values
- Corrupt accounting variables (balances, allowances) before the constructor runs
- Cause the deployed contract to behave as if it was already in a post-initialization state

This is a **state-transition bug / EVM semantic mismatch** with direct funds-loss potential for any contract deployed via CREATE2 to a previously-used address.

---

### Likelihood Explanation

The attack requires:
1. A prior contract at the target CREATE2 address that was selfdestructed (leaving storage behind).
2. The victim to redeploy to the same address via CREATE2.

This is a realistic scenario in:
- **Counterfactual deployment patterns** (e.g., account abstraction wallets, Layer 2 bridges) where the same salt/deployer pair is reused after a prior deployment is destroyed.
- **Proxy factory patterns** where CREATE2 is used with deterministic salts and a prior proxy was selfdestructed.
- **Upgrade patterns** that destroy and redeploy contracts at fixed addresses.

Likelihood is **medium**: it requires a specific prior state (selfdestructed contract with residual storage), but this is a well-known pattern in production DeFi and AA infrastructure.

---

### Recommendation

Implement the storage-emptiness check as part of the CREATE/CREATE2 collision guard in `before_executing_frame`. Before allowing a constructor frame to execute, verify that the target address has no non-zero storage slots. This requires the IO subsystem to expose a method to check whether an address has any non-zero storage (e.g., a storage root hash check or an explicit "has any storage" query). Until this is implemented, ZKsync OS diverges from EIP-7610 semantics and is vulnerable to storage-collision attacks on CREATE2 deployments.

---

### Proof of Concept

1. Attacker deploys contract `V1` at address `A` using CREATE2 with salt `S` from deployer `D`. `V1` writes `storage[0] = attacker_address` in its constructor.
2. Attacker calls `V1.selfdestruct()`. Address `A` now has: `code = empty`, `nonce = 0`, `storage[0] = attacker_address`.
3. Victim calls CREATE2 with the same deployer `D` and salt `S` to deploy contract `V2` (e.g., a proxy or wallet).
4. ZKsync OS checks: `deployee_code_len == 0` ✓ and `deployee_nonce == 0` ✓ → collision check passes, constructor executes.
5. `V2` is deployed at address `A` with `storage[0] = attacker_address` already set.
6. If `V2` uses `storage[0]` as its owner/admin slot (common in proxy patterns), the attacker is now the owner of `V2` without ever interacting with it post-deployment.

The root cause is at: [1](#0-0)

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

**File:** tests/evm_tester/indexes/develop-blockchain-tests.yaml (L2737-2740)
```yaml
              RevertInCreateInInitCreate2Paris.json:
                hash: '0xd89c5a3c6ccc430e2e4a0c6bb5d2dd01'
                enabled: false
                comment: We do not check for storage collisions
```

**File:** tests/evm_tester/indexes/develop-state-tests.yaml (L607-617)
```yaml
      eip7610_create_collision:
        enabled: true
        entries:
          test_init_collision_create_opcode.json:
            hash: '0x984bd54463558e4238b4dcb0d73178fa'
            enabled: false
            comment: We do not check the storage
          test_init_collision_create_tx.json:
            hash: '0x1e649d672fc3506e91a91d945a8b3960'
            enabled: false
            comment: We do not check the storage
```

**File:** tests/evm_tester/indexes/develop-state-tests.yaml (L2367-2370)
```yaml
              create2collisionStorageParis.json:
                hash: '0x1c8ea0d96bc40fd2a8997e58950d6bdb'
                enabled: false
                comment: We do not check storage for collision
```

**File:** basic_bootloader/src/bootloader/config.rs (L1-40)
```rust
pub trait BasicBootloaderExecutionConfig: 'static + Clone + Copy + core::fmt::Debug {
    /// Flag to disable EOA signature validation.
    /// It can be used to optimize forward run.
    const VALIDATE_EOA_SIGNATURE: bool;
    /// Simulation flag(used for `eth_call` and `estimate_gas`)
    const SIMULATION: bool;
}

#[derive(Clone, Copy, Debug)]
pub struct BasicBootloaderProvingExecutionConfig;

impl BasicBootloaderExecutionConfig for BasicBootloaderProvingExecutionConfig {
    const SIMULATION: bool = false;
    const VALIDATE_EOA_SIGNATURE: bool = true;
}

#[derive(Clone, Copy, Debug)]
pub struct BasicBootloaderForwardSimulationConfig;

impl BasicBootloaderExecutionConfig for BasicBootloaderForwardSimulationConfig {
    const VALIDATE_EOA_SIGNATURE: bool = false;
    const SIMULATION: bool = false;
}

#[derive(Clone, Copy, Debug)]
pub struct BasicBootloaderCallSimulationConfig;

impl BasicBootloaderExecutionConfig for BasicBootloaderCallSimulationConfig {
    // doesn't really matter, as `SIMULATION` disables signature validation anyway
    const VALIDATE_EOA_SIGNATURE: bool = true;
    const SIMULATION: bool = true;
}

#[derive(Clone, Copy, Debug)]
pub struct BasicBootloaderForwardETHLikeConfig;

impl BasicBootloaderExecutionConfig for BasicBootloaderForwardETHLikeConfig {
    const VALIDATE_EOA_SIGNATURE: bool = true;
    const SIMULATION: bool = false;
}
```
