### Title
Owner Can Bypass `upgrade_delay_blocks` Timelock via Direct `upgrade()` Path, Enabling Immediate Arbitrary Code Deployment - (File: `engine/src/contract_methods/admin.rs`)

---

### Summary

The Aurora Engine exposes two distinct upgrade paths. The `stage_upgrade` + `deploy_upgrade` path enforces `upgrade_delay_blocks` as a timelock. However, the direct `upgrade()` function deploys arbitrary new WASM code to the engine contract **immediately**, with no delay check whatsoever. Because the owner can also reduce `upgrade_delay_blocks` to zero at will with no minimum enforced, the timelock protection is entirely illusory. A malicious or compromised owner can deploy code that drains all ETH and NEP-141 tokens held by the engine in a single atomic block.

---

### Finding Description

There are two upgrade paths in the engine:

**Path A — Timelocked (stage + deploy):**

`stage_upgrade()` records `block_height + upgrade_delay_blocks` as the earliest allowed deployment block. [1](#0-0) 

`deploy_upgrade()` enforces the delay before self-deploying: [2](#0-1) 

**Path B — No timelock (direct `upgrade()`):**

The `upgrade()` function accepts arbitrary WASM code and immediately issues a `DeployContract` promise with no block-height check: [3](#0-2) 

The only guard is `require_owner_only`. There is no reference to `upgrade_delay_blocks`, no staged index check, and no minimum delay: [4](#0-3) 

Additionally, `set_upgrade_delay_blocks()` accepts any `u64` value including zero, with no minimum enforced: [5](#0-4) 

The `EngineState` field `upgrade_delay_blocks` is documented as a protection mechanism, but it only applies to Path A: [6](#0-5) 

A second compounding vector is `attach_full_access_key()`, which the owner can call to add a full access key to the engine's NEAR account — granting that key unrestricted ability to transfer all NEAR held by the engine: [7](#0-6) 

---

### Impact Explanation

**Critical — Direct theft of user funds.**

Users deposit ETH into the Aurora Engine via the ETH connector. The engine holds the corresponding NEP-141 balances. A malicious owner can call `upgrade()` with a crafted WASM binary that, in its `state_migration` callback or any subsequent call, transfers all ETH-backed balances to an attacker-controlled address. Because `upgrade()` issues a `DeployContract` promise in the same transaction, the new code is live in the very next block — no delay, no warning, no recourse for users. The `upgrade_delay_blocks` value displayed on-chain gives users a false sense of security.

---

### Likelihood Explanation

**Medium.** The attack requires the `owner_id` account to act maliciously or be compromised (e.g., private key leak, social engineering of a single EOA owner). The Aurora Engine is deployed at `aurora` on NEAR mainnet and holds significant user ETH. If the owner is a single account rather than a multisig, the single point of failure is realistic. The M-12 report was validated on exactly this basis: users approve/deposit funds to the contract, making the owner's unchecked power a live risk.

---

### Recommendation

1. **Remove or gate the direct `upgrade()` path.** Either delete `upgrade()` entirely and require all upgrades to go through `stage_upgrade` + `deploy_upgrade`, or add the same block-height delay check that `deploy_upgrade()` enforces.
2. **Enforce a minimum `upgrade_delay_blocks`.** `set_upgrade_delay_blocks()` should reject values below a protocol-defined minimum (e.g., 1000 blocks ≈ ~12 hours on NEAR).
3. **Require a multisig for `owner_id`.** A single-account owner is a single point of failure. The owner should be a DAO or M-of-N multisig.
4. **Remove or restrict `attach_full_access_key()`.** Adding a full access key to the engine account is an unrestricted fund-drain vector; it should require a timelock or be removed entirely.

---

### Proof of Concept

```
// Attacker controls owner_id (or owner_id is compromised)

// Step 1: craft malicious WASM that, on state_migration, calls
//         ft_transfer_call to move all ETH-backed NEP-141 to attacker

// Step 2: call upgrade() with the malicious WASM — no delay check
aurora.upgrade(malicious_wasm_bytes)
  // internally: PromiseAction::DeployContract { code: malicious_wasm_bytes }
  //             PromiseAction::FunctionCall { name: "state_migration", ... }

// Step 3: state_migration executes in the same block, draining all user funds

// No stage_upgrade, no deploy_upgrade, no block-height check, no warning.
// upgrade_delay_blocks is completely bypassed.
```

The direct `upgrade()` entrypoint is exposed at: [8](#0-7)

### Citations

**File:** engine/src/contract_methods/admin.rs (L135-144)
```rust
pub fn set_upgrade_delay_blocks<I: IO + Copy, E: Env>(io: I, env: &E) -> Result<(), ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        let mut state = state::get_state(&io)?;
        require_running(&state)?;
        require_owner_only(&state, &env.predecessor_account_id())?;
        let args: SetUpgradeDelayBlocksArgs = io.read_input_borsh()?;
        state.upgrade_delay_blocks = args.upgrade_delay_blocks;
        state::set_state(&mut io, &state)?;
        Ok(())
    })
```

**File:** engine/src/contract_methods/admin.rs (L154-167)
```rust
pub fn stage_upgrade<I: IO + Copy, E: Env>(io: I, env: &E) -> Result<(), ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;
        let delay_block_height = env.block_height() + state.upgrade_delay_blocks;
        require_owner_only(&state, &env.predecessor_account_id())?;
        io.read_input_and_store(&storage::bytes_to_key(KeyPrefix::Config, CODE_KEY));
        io.write_storage(
            &storage::bytes_to_key(KeyPrefix::Config, CODE_STAGE_KEY),
            &delay_block_height.to_le_bytes(),
        );
        Ok(())
    })
}
```

**File:** engine/src/contract_methods/admin.rs (L169-206)
```rust
pub fn upgrade<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<(), ContractError> {
    let state = state::get_state(&io)?;
    require_running(&state)?;
    require_owner_only(&state, &env.predecessor_account_id())?;

    let input = io.read_input().to_vec();
    let (code, state_migration_gas) = match UpgradeParams::try_from_slice(&input) {
        Ok(args) => (
            args.code,
            args.state_migration_gas
                .map_or(GAS_FOR_STATE_MIGRATION, NearGas::new),
        ),
        Err(_) => (input, GAS_FOR_STATE_MIGRATION), // Backward compatibility
    };

    let target_account_id = env.current_account_id();
    let batch = PromiseBatchAction {
        target_account_id,
        actions: vec![
            PromiseAction::DeployContract { code },
            PromiseAction::FunctionCall {
                name: "state_migration".to_string(),
                args: vec![],
                attached_yocto: ZERO_YOCTO,
                gas: state_migration_gas,
            },
        ],
    };
    let promise_id = handler.promise_create_batch(&batch);

    handler.promise_return(promise_id);

    Ok(())
}
```

**File:** engine/src/contract_methods/admin.rs (L483-512)
```rust
pub fn attach_full_access_key<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<(), ContractError> {
    let state = state::get_state(&io)?;

    require_running(&state)?;
    require_owner_only(&state, &env.predecessor_account_id())?;

    let public_key = serde_json::from_slice::<FullAccessKeyArgs>(&io.read_input().to_vec())
        .map(|args| args.public_key)
        .map_err(|_| errors::ERR_JSON_DESERIALIZE)?;
    let current_account_id = env.current_account_id();
    let action = PromiseAction::AddFullAccessKey {
        public_key,
        nonce: 0, // not actually used - depends on block height
    };
    let promise = PromiseBatchAction {
        target_account_id: current_account_id,
        actions: vec![action],
    };
    // SAFETY: This action is dangerous because it adds a new full access key (FAK) to the Engine account.
    // However, it is safe to do so here because of the `require_owner_only` check above; only the
    // (trusted) owner account can add a new FAK.
    let promise_id = handler.promise_create_batch(&promise);

    handler.promise_return(promise_id);

    Ok(())
```

**File:** engine/src/lib.rs (L147-157)
```rust
    /// Upgrade the contract with the provided code bytes.
    #[unsafe(no_mangle)]
    pub extern "C" fn upgrade() {
        let io = Runtime;
        let env = Runtime;
        let mut handler = Runtime;

        contract_methods::admin::upgrade(io, &env, &mut handler)
            .map_err(ContractError::msg)
            .sdk_unwrap();
    }
```

**File:** engine/src/lib.rs (L171-185)
```rust
    pub extern "C" fn deploy_upgrade() {
        // This function is intentionally not implemented in `contract_methods`
        // because it only makes sense in the context of the NEAR runtime.
        let mut io = Runtime;
        let state = state::get_state(&io).sdk_unwrap();
        require_running(&state)
            .map_err(ContractError::msg)
            .sdk_unwrap();
        let index = internal_get_upgrade_index();
        if io.block_height() <= index {
            sdk::panic_utf8(errors::ERR_NOT_ALLOWED_TOO_EARLY);
        }
        Runtime::self_deploy(&bytes_to_key(KeyPrefix::Config, CODE_KEY));
        io.remove_storage(&bytes_to_key(KeyPrefix::Config, CODE_STAGE_KEY));
    }
```

**File:** engine/src/state.rs (L25-27)
```rust
    /// How many blocks after staging upgrade can deploy it.
    pub upgrade_delay_blocks: u64,
    /// Flag to pause and unpause the engine.
```
