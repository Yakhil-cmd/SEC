### Title
Owner Can Bypass `upgrade_delay_blocks` and Deploy Arbitrary Contract Code Immediately - (`engine/src/contract_methods/admin.rs`)

### Summary

The Aurora Engine has two distinct upgrade paths: a delayed path (`stage_upgrade` → `deploy_upgrade`) that enforces `upgrade_delay_blocks`, and a direct path (`upgrade`) that completely ignores the delay. The `upgrade` function allows the owner to immediately deploy arbitrary new contract bytecode to the engine without waiting for the configured delay period, defeating the purpose of the `upgrade_delay_blocks` security parameter.

### Finding Description

The `EngineState` stores an `upgrade_delay_blocks` field, which is the number of blocks that must pass after staging before a new contract can be deployed. The intended upgrade flow is:

1. Owner calls `stage_upgrade` — stores the new code and records `delay_block_height = block_height + upgrade_delay_blocks`.
2. Anyone calls `deploy_upgrade` — checks `block_height > index` before deploying.

However, a second, parallel upgrade entrypoint exists: the `upgrade` function in `engine/src/contract_methods/admin.rs`. This function checks only `require_running` and `require_owner_only`, then immediately issues a `PromiseBatchAction` with `PromiseAction::DeployContract { code }` — with **no check against the staged index or any delay**.

```rust
pub fn upgrade<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I, env: &E, handler: &mut H,
) -> Result<(), ContractError> {
    let state = state::get_state(&io)?;
    require_running(&state)?;
    require_owner_only(&state, &env.predecessor_account_id())?;
    // ... directly deploys with no delay check
    let batch = PromiseBatchAction {
        target_account_id,
        actions: vec![
            PromiseAction::DeployContract { code },
            ...
        ],
    };
```

The `deploy_upgrade` path enforces the delay:

```rust
let index = internal_get_upgrade_index();
if io.block_height() <= index {
    sdk::panic_utf8(errors::ERR_NOT_ALLOWED_TOO_EARLY);
}
```

But `upgrade` never calls `internal_get_upgrade_index` at all. The `upgrade_delay_blocks` parameter is entirely bypassed.

### Impact Explanation

The `upgrade_delay_blocks` mechanism exists to give users a reaction window before a new engine version takes effect. The Aurora Engine controls all EVM state, user balances, and bridge operations for the entire Aurora ecosystem. An owner who calls `upgrade` directly can immediately replace the engine contract with arbitrary bytecode — including code that steals all bridged ETH/ERC-20 balances or permanently freezes all user funds — without any delay window for users to exit.

This is a **critical** impact: direct theft of all user funds at rest, or permanent freezing of funds.

### Likelihood Explanation

The `upgrade` function is a publicly documented, reachable NEAR contract method exposed via `#[unsafe(no_mangle)] pub extern "C" fn upgrade()`. Any call from the owner account triggers it immediately. The owner account is a single NEAR account; if it is compromised (e.g., leaked key, social engineering of the key holder), the attacker can deploy malicious code in a single transaction with no delay window for users to respond.

### Recommendation

Remove the `upgrade` function entirely, or override it to require the same staged delay as `deploy_upgrade`. All contract upgrades should go through `stage_upgrade` followed by `deploy_upgrade` after the delay has elapsed. The `upgrade` function should not exist as a parallel bypass path.

### Proof of Concept

**Root cause — `upgrade` bypasses delay:** [1](#0-0) 

**`deploy_upgrade` correctly enforces the delay:** [2](#0-1) 

**`stage_upgrade` sets the delay block height:** [3](#0-2) 

**`upgrade_delay_blocks` is a security parameter in `EngineState`:** [4](#0-3) 

**The `upgrade` entrypoint is exposed as a public NEAR contract method:** [5](#0-4) 

**Attack path:**
1. Owner account (or attacker who has compromised it) calls `upgrade` with malicious bytecode.
2. `require_owner_only` passes; no delay check is performed.
3. `PromiseAction::DeployContract` immediately replaces the engine contract.
4. The new contract can drain all bridged ETH/ERC-20 balances or freeze all user funds.
5. Users have zero blocks of warning — the `upgrade_delay_blocks` parameter is completely ignored.

### Citations

**File:** engine/src/contract_methods/admin.rs (L153-167)
```rust
#[named]
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

**File:** engine/src/lib.rs (L169-185)
```rust
    /// Deploy staged upgrade.
    #[unsafe(no_mangle)]
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

**File:** engine/src/state.rs (L25-26)
```rust
    /// How many blocks after staging upgrade can deploy it.
    pub upgrade_delay_blocks: u64,
```
