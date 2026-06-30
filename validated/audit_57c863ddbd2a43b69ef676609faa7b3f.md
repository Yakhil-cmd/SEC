### Title
Unprotected `new()` Initialization Allows Any Caller to Seize Engine Ownership - (File: engine/src/contract_methods/admin.rs)

### Summary
The Aurora Engine's `new()` initialization function contains no caller access control. Any NEAR account can call it before the legitimate deployer does, setting an arbitrary `owner_id` and gaining full administrative control over the Aurora EVM runtime.

### Finding Description
The `new()` function in `engine/src/contract_methods/admin.rs` is the sole initialization entry point for the Aurora Engine contract. Its only guard is a check that the state has not already been written:

```rust
pub fn new<I: IO + Copy, E: Env>(mut io: I, env: &E) -> Result<(), ContractError> {
    if state::get_state(&io).is_ok() {
        return Err(b"ERR_ALREADY_INITIALIZED".into());
    }
    // No predecessor_account_id check
    let input = io.read_input().to_vec();
    let args = NewCallArgs::deserialize(&input)...;
    let state: EngineState = args.into();
    state::set_state(&mut io, &state)?;
    Ok(())
}
``` [1](#0-0) 

The `NewCallArgs` struct includes `owner_id`, `chain_id`, and `upgrade_delay_blocks`. An attacker who calls `new()` first can supply their own account as `owner_id`.

The workspace deployment code confirms that WASM deployment and `new()` are issued as **separate transactions** with an `await` between them, creating a front-runnable window:

```rust
let contract = account.deploy(&self.code...).await?;
// window exists here
engine.new(self.chain_id, self.owner_id, self.upgrade_delay_blocks).transact().await...
``` [2](#0-1) 

Every other privileged function in the admin module correctly calls `require_owner_only` before acting: [3](#0-2) 

But `new()` has no such check.

### Impact Explanation
An attacker who front-runs `new()` becomes the `owner_id` of the Aurora Engine. As owner they can:

- Call `stage_upgrade` (owner-only) to stage arbitrary WASM code, then trigger `deploy_upgrade` (open to anyone) to replace the engine with malicious code. [4](#0-3) 
- Call `attach_full_access_key` (owner-only) to add a full-access key to the `aurora` NEAR account, giving permanent unrestricted control. [5](#0-4) 
- Call `pause_contract` to permanently freeze all user funds. [6](#0-5) 

The Aurora Engine holds all ETH and ERC-20 tokens bridged from Ethereum. A malicious owner can drain or permanently freeze all of these funds. Impact: **Critical — direct theft of all user funds and/or permanent fund freeze**.

### Likelihood Explanation
NEAR transactions are publicly observable. An attacker monitoring the NEAR blockchain for a new deployment of the Aurora Engine WASM (a `DeployContract` action on the target account) can immediately submit a `new()` call with a malicious `owner_id` before the legitimate deployer's initialization transaction is included. The deployment and initialization are confirmed to be separate transactions in the workspace code. No special privileges or leaked keys are required — only the ability to submit a NEAR transaction.

### Recommendation
Add a caller check inside `new()` that restricts initialization to the contract account itself (i.e., `env.predecessor_account_id() == env.current_account_id()`), or use a NEAR batch transaction that atomically combines `DeployContract` and the `new()` call so no window exists between deployment and initialization.

### Proof of Concept
1. Attacker monitors NEAR for a `DeployContract` action targeting the `aurora` account.
2. Attacker immediately submits a call to `aurora::new` with `NewCallArgs { owner_id: "attacker.near", chain_id: ..., upgrade_delay_blocks: 0 }`.
3. If the attacker's transaction is included before the legitimate deployer's `new()` call, `state::set_state` writes `owner_id = "attacker.near"`.
4. The legitimate deployer's `new()` call reverts with `ERR_ALREADY_INITIALIZED`. [7](#0-6) 
5. Attacker calls `stage_upgrade` with malicious WASM, then `deploy_upgrade` (callable by anyone) to replace the engine. [8](#0-7) 
6. All bridged ETH and ERC-20 tokens are now under attacker control.

### Citations

**File:** engine/src/contract_methods/admin.rs (L55-88)
```rust
#[named]
pub fn new<I: IO + Copy, E: Env>(mut io: I, env: &E) -> Result<(), ContractError> {
    if state::get_state(&io).is_ok() {
        return Err(b"ERR_ALREADY_INITIALIZED".into());
    }

    let input = io.read_input().to_vec();
    let args = NewCallArgs::deserialize(&input).map_err(|_| errors::ERR_BORSH_DESERIALIZE)?;

    let initial_hashchain = args.initial_hashchain();
    let state: EngineState = args.into();

    if let Some(block_hashchain) = initial_hashchain {
        let block_height = env.block_height();
        let mut hashchain = Hashchain::new(
            state.chain_id,
            env.current_account_id(),
            block_height,
            block_hashchain,
        );

        hashchain.add_block_tx(
            block_height,
            function_name!(),
            &input,
            &[],
            &Bloom::default(),
        )?;
        crate::hashchain::save_hashchain(&mut io, &hashchain)?;
    }

    state::set_state(&mut io, &state)?;
    Ok(())
}
```

**File:** engine/src/contract_methods/admin.rs (L103-121)
```rust
#[named]
pub fn set_owner<I: IO + Copy, E: Env>(io: I, env: &E) -> Result<(), ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        let mut state = state::get_state(&io)?;

        require_running(&state)?;
        require_owner_only(&state, &env.predecessor_account_id())?;

        let args: SetOwnerArgs = io.read_input_borsh()?;
        if state.owner_id == args.new_owner {
            return Err(errors::ERR_SAME_OWNER.into());
        }

        state.owner_id = args.new_owner;
        state::set_state(&mut io, &state)?;

        Ok(())
    })
}
```

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

**File:** engine/src/contract_methods/admin.rs (L250-260)
```rust
#[named]
pub fn pause_contract<I: IO + Copy, E: Env>(io: I, env: &E) -> Result<(), ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        let mut state = state::get_state(&io)?;
        require_owner_only(&state, &env.predecessor_account_id())?;
        require_running(&state)?;
        state.is_paused = true;
        state::set_state(&mut io, &state)?;
        Ok(())
    })
}
```

**File:** engine/src/contract_methods/admin.rs (L483-513)
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
}
```

**File:** engine-workspace/src/lib.rs (L107-125)
```rust
        let contract = account
            .deploy(
                &self
                    .code
                    .ok_or_else(|| anyhow::anyhow!("WASM wasn't set"))?,
            )
            .await?;
        let engine = EngineContract {
            account,
            contract,
            public_key,
            node,
        };

        engine
            .new(self.chain_id, self.owner_id, self.upgrade_delay_blocks)
            .transact()
            .await
            .map_err(|e| anyhow::anyhow!("Error while initialize aurora contract: {e}"))?;
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
