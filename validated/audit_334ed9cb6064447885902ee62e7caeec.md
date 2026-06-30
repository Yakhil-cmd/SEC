### Title
Owner Can Instantly Zero Out `upgrade_delay_blocks` to Bypass the Upgrade Timelock and Deploy Arbitrary Code - (File: `engine/src/contract_methods/admin.rs`)

---

### Summary

`set_upgrade_delay_blocks` accepts any value including zero and takes effect immediately with no minimum bound and no delay on the change itself. An owner can atomically reduce the delay to zero, stage malicious upgrade code, and deploy it — all within the same block — giving users zero time to observe and exit before a hostile contract replacement.

---

### Finding Description

`EngineState` carries `upgrade_delay_blocks` as a public, observable security parameter. Its documented purpose is to enforce a waiting period between staging an upgrade and deploying it, so that users can react to undesirable changes. [1](#0-0) 

`stage_upgrade` computes the earliest deployable block as `current_block + upgrade_delay_blocks` and writes it to `CODE_STAGE_KEY`: [2](#0-1) 

However, `set_upgrade_delay_blocks` imposes **no minimum value** and **no delay on the change itself**. It writes the new value directly to state in the same transaction: [3](#0-2) 

Additionally, the `upgrade` function — which performs the actual contract deployment — reads code from calldata and deploys it **without ever consulting `CODE_STAGE_KEY`** or verifying that the current block height has passed the staged delay: [4](#0-3) 

These two weaknesses compound: the owner can first collapse the delay to zero via `set_upgrade_delay_blocks`, then call `stage_upgrade` (which now records `delay_block_height = current_block`), and immediately call `upgrade` with arbitrary replacement bytecode — all in the same block.

---

### Impact Explanation

**Critical — Direct theft of all user funds.**

The Aurora Engine holds all bridged ETH and ERC-20 token balances for every address in the EVM. A hostile contract deployment can rewrite any storage slot, redirect withdrawals, or drain the ETH connector. Every user who has deposited assets into the Aurora EVM is exposed. There is no on-chain mechanism that prevents the owner from executing this sequence atomically.

---

### Likelihood Explanation

**Medium.** The attack requires the owner account to be compromised or act maliciously. However, the upgrade delay is the *only* user-facing protection against a hostile upgrade; once it is zeroed out, no other safeguard exists. The `set_upgrade_delay_blocks` call is a single, low-cost NEAR transaction. Users monitoring the hashchain would see the change only after it has already taken effect, leaving no reaction window.

---

### Recommendation

1. **Enforce a minimum value** in `set_upgrade_delay_blocks` — reject any value below a protocol-defined floor (e.g., the current `upgrade_delay_blocks`).
2. **Apply the delay to the delay-change itself** — treat a reduction in `upgrade_delay_blocks` as a staged operation subject to the *existing* delay before it takes effect.
3. **Make `upgrade` enforce the staged block height** — `upgrade` must read `CODE_STAGE_KEY`, verify `env.block_height() >= staged_block_height`, and deploy only the code stored at `CODE_KEY` rather than accepting arbitrary calldata.

---

### Proof of Concept

```
Block N:
  owner → set_upgrade_delay_blocks({ upgrade_delay_blocks: 0 })
    // state.upgrade_delay_blocks = 0, effective immediately

Block N (same or next):
  owner → stage_upgrade(<malicious_wasm>)
    // delay_block_height = block_height + 0 = block_height
    // CODE_KEY      ← malicious_wasm
    // CODE_STAGE_KEY ← block_height  (already satisfied)

Block N (same or next):
  owner → upgrade(<malicious_wasm>)
    // No check of CODE_STAGE_KEY; deploys malicious_wasm immediately
    // PromiseAction::DeployContract { code: malicious_wasm }
    // All user ETH and ERC-20 balances now under attacker control
```

The three calls can be submitted in a single NEAR block. Users relying on the previously-advertised `upgrade_delay_blocks` value receive zero warning.

### Citations

**File:** engine/src/state.rs (L25-26)
```rust
    /// How many blocks after staging upgrade can deploy it.
    pub upgrade_delay_blocks: u64,
```

**File:** engine/src/contract_methods/admin.rs (L134-145)
```rust
#[named]
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
}
```

**File:** engine/src/contract_methods/admin.rs (L154-166)
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
