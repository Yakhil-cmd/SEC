### Title
Unprotected `new()` Initialization Allows Any Caller to Seize Engine Ownership - (File: `engine/src/contract_methods/admin.rs`)

### Summary
The `new()` function that initializes the Aurora Engine's state performs no caller authentication. Any NEAR account that calls it before the legitimate deployer can set themselves as `owner_id` and `key_manager`, gaining full administrative control over the engine, including the ability to upgrade the contract to arbitrary code and steal all bridged funds.

### Finding Description
The `new()` function in `engine/src/contract_methods/admin.rs` is the sole initialization entry point for the Aurora Engine contract. It accepts caller-supplied `owner_id` and `key_manager` fields from the input arguments and writes them directly into the engine state, with no check that the caller is the contract account itself or any other privileged identity.

The only guard present is a re-initialization check:

```rust
pub fn new<I: IO + Copy, E: Env>(mut io: I, env: &E) -> Result<(), ContractError> {
    if state::get_state(&io).is_ok() {
        return Err(b"ERR_ALREADY_INITIALIZED".into());
    }
    // ...
    let state: EngineState = args.into();  // owner_id taken directly from args
    state::set_state(&mut io, &state)?;
    Ok(())
}
``` [1](#0-0) 

The `owner_id` stored in `EngineState` is taken verbatim from the caller-supplied `NewCallArgs`: [2](#0-1) 

The deployment flow in the workspace helper performs contract deployment and initialization as two separate awaited transactions, creating an observable window: [3](#0-2) 

Between the `deploy` call completing and the `new(...)` call being submitted, any NEAR account can race to call `new()` with their own `owner_id` and `key_manager`.

### Impact Explanation
The `owner_id` stored during `new()` is the sole authority checked by every privileged administrative function: `set_owner`, `stage_upgrade`, `upgrade`, `pause_contract`, `resume_contract`, `set_key_manager`, `set_upgrade_delay_blocks`, and `mirror_erc20_token`. [4](#0-3) 

The most severe consequence is the `upgrade()` function, which is gated only by `require_owner_only`. An attacker who controls `owner_id` can call `upgrade()` to deploy arbitrary WASM code to the Aurora Engine account, replacing the entire contract logic. This allows direct theft of all bridged ETH and ERC-20 token balances held by the engine. [5](#0-4) 

**Impact**: Critical — direct theft of all user funds held by the Aurora Engine.

### Likelihood Explanation
On NEAR, a contract deploy action and a subsequent function call are separate transactions unless explicitly batched into a single `PromiseBatchAction`. The workspace deployment helper issues them as two separate awaited async calls. An attacker monitoring the NEAR blockchain for a freshly deployed Aurora Engine contract (zero state) can submit a `new()` call in the same or next block before the legitimate deployer's initialization transaction is processed. No special privileges are required — any NEAR account with enough gas can execute this.

### Recommendation
Add a check at the start of `new()` that asserts `env.predecessor_account_id() == env.current_account_id()`. This ensures only the contract account itself (i.e., the deployer acting via a batch transaction) can initialize the engine state:

```rust
pub fn new<I: IO + Copy, E: Env>(mut io: I, env: &E) -> Result<(), ContractError> {
    if env.predecessor_account_id() != env.current_account_id() {
        return Err(b"ERR_NOT_ALLOWED".into());
    }
    if state::get_state(&io).is_ok() {
        return Err(b"ERR_ALREADY_INITIALIZED".into());
    }
    // ...
}
```

Deployment should also be done as a single atomic NEAR batch action (deploy + `new` call) to eliminate the race window entirely.

### Proof of Concept
1. Attacker monitors NEAR for a newly deployed Aurora Engine contract where `get_state` returns `ERR_STATE_NOT_FOUND`.
2. Attacker submits a call to `new()` with `owner_id = "attacker.near"` and `key_manager = "attacker.near"` before the legitimate deployer's `new()` transaction is processed.
3. `state::get_state(&io).is_ok()` returns `Err` (not yet initialized), so the guard passes.
4. `state::set_state` writes `owner_id = "attacker.near"` into contract storage.
5. The legitimate deployer's `new()` call now fails with `ERR_ALREADY_INITIALIZED`.
6. Attacker calls `upgrade()` (gated only by `require_owner_only`) to deploy a malicious WASM contract that drains all bridged ETH and ERC-20 balances. [1](#0-0) [5](#0-4)

### Citations

**File:** engine/src/contract_methods/admin.rs (L56-88)
```rust
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

**File:** engine/src/contract_methods/admin.rs (L169-205)
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
```

**File:** engine/src/state.rs (L184-194)
```rust
impl From<NewCallArgsV4> for EngineState {
    fn from(args: NewCallArgsV4) -> Self {
        Self {
            chain_id: args.chain_id,
            owner_id: args.owner_id,
            upgrade_delay_blocks: args.upgrade_delay_blocks,
            is_paused: false,
            key_manager: Some(args.key_manager),
        }
    }
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

**File:** engine/src/contract_methods/mod.rs (L79-87)
```rust
pub fn require_owner_only(
    state: &state::EngineState,
    predecessor_account_id: &AccountId,
) -> Result<(), ContractError> {
    if &state.owner_id != predecessor_account_id {
        return Err(errors::ERR_NOT_ALLOWED.into());
    }
    Ok(())
}
```
