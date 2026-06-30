### Title
Unprotected `new` Initializer Allows Front-Running to Seize Full Engine Ownership — (File: `engine/src/contract_methods/admin.rs`)

---

### Summary

The Aurora Engine's `new` initialization function performs no caller authentication. Any NEAR account can call it on an uninitialized contract. Because the workspace deployment code issues the contract deployment and the `new` call as two separate transactions, an attacker can observe the deployment and race to call `new` first — setting themselves as `owner_id` and permanently seizing administrative control of the engine before the legitimate deployer can initialize it.

---

### Finding Description

The `new` function in `engine/src/contract_methods/admin.rs` is the sole initialization entry point for the Aurora Engine. Its only guard is a check that the contract has not already been initialized:

```rust
pub fn new<I: IO + Copy, E: Env>(mut io: I, env: &E) -> Result<(), ContractError> {
    if state::get_state(&io).is_ok() {
        return Err(b"ERR_ALREADY_INITIALIZED".into());
    }
    let input = io.read_input().to_vec();
    let args = NewCallArgs::deserialize(&input)...;
    // ...
    state::set_state(&mut io, &state)?;
    Ok(())
}
``` [1](#0-0) 

There is no check on `env.predecessor_account_id()`. The `owner_id` field that controls all subsequent administrative authority is taken entirely from the caller-supplied input args (`NewCallArgs`), with no restriction on who may supply them. [2](#0-1) 

The workspace deployment helper — which reflects the real deployment flow — issues the WASM deployment and the `new` call as two distinct, sequential transactions:

```rust
let contract = account.deploy(&self.code...).await?;
// ...
engine.new(self.chain_id, self.owner_id, self.upgrade_delay_blocks)
    .transact()
    .await?;
``` [3](#0-2) 

Between the `deploy` receipt and the `new` receipt there is an observable window on the NEAR blockchain. An attacker who monitors for a new contract deployment at the `aurora` account can submit their own `new` call with `owner_id = attacker` and have it land before the legitimate deployer's call. The legitimate deployer's subsequent `new` call then fails with `ERR_ALREADY_INITIALIZED`, and the attacker is permanently installed as owner.

The public NEAR entrypoint is:

```rust
#[unsafe(no_mangle)]
pub extern "C" fn new() {
    let io = Runtime;
    let env = Runtime;
    contract_methods::admin::new(io, &env)...
}
``` [4](#0-3) 

---

### Impact Explanation

The `owner_id` set during `new` is the account that controls:
- Contract upgrades (`stage_upgrade` / `upgrade`) — enabling arbitrary code replacement
- Pausing and resuming the engine and all precompiles
- Setting a new owner, key manager, and relayer keys
- All other privileged administrative operations [5](#0-4) 

An attacker who seizes ownership can immediately pause the engine (freezing all user funds permanently) or stage and deploy a malicious upgrade that drains all bridged ETH and ERC-20 balances. This satisfies both **Critical: Permanent freezing of funds** and **Critical: Direct theft of any user funds**.

---

### Likelihood Explanation

NEAR Protocol transactions are publicly observable before finalization. The deployment of a new WASM binary to the `aurora` account is a distinct, visible on-chain event. The window between that event and the subsequent `new` call is at minimum one block (~1 second on NEAR mainnet). An attacker running a monitoring bot can detect the deployment receipt and submit a competing `new` call within that window. This is directly analogous to Ethereum front-running and requires no privileged access — only the ability to submit a NEAR transaction.

---

### Recommendation

Add a caller check inside `new` so that only the contract account itself (i.e., a self-call from a batch that also deployed the code) may initialize it:

```rust
pub fn new<I: IO + Copy, E: Env>(mut io: I, env: &E) -> Result<(), ContractError> {
    if state::get_state(&io).is_ok() {
        return Err(b"ERR_ALREADY_INITIALIZED".into());
    }
    // Add: only the contract itself may initialize (enforces atomic deploy+init batch)
    if env.predecessor_account_id() != env.current_account_id() {
        return Err(b"ERR_NOT_ALLOWED".into());
    }
    // ...
}
```

Alternatively, enforce that deployment and initialization are always performed in a single atomic NEAR batch transaction (deploy contract + call `new` in one batch action), which eliminates the inter-transaction window entirely. The XCC router already uses this pattern correctly for its own `initialize`. [6](#0-5) 

---

### Proof of Concept

1. Attacker subscribes to NEAR RPC events for the `aurora` account.
2. Aurora team submits a transaction deploying new engine WASM to `aurora`.
3. Attacker observes the deployment receipt in the next block.
4. Attacker immediately submits a NEAR transaction calling `aurora::new` with:
   ```json
   { "owner_id": "attacker.near", "chain_id": [...], "upgrade_delay_blocks": 0 }
   ```
5. Attacker's `new` call executes before the Aurora team's `new` call (no caller restriction, contract not yet initialized).
6. Aurora team's `new` call returns `ERR_ALREADY_INITIALIZED` and fails.
7. Attacker calls `stage_upgrade` + `upgrade` with malicious WASM, or calls `pause_contract`, permanently freezing all user funds or stealing them via the upgraded contract. [1](#0-0) [3](#0-2)

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

**File:** engine/src/contract_methods/admin.rs (L153-206)
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

**File:** engine-types/src/parameters/engine.rs (L76-85)
```rust
#[derive(Debug, Clone, PartialEq, Eq, BorshSerialize, BorshDeserialize)]
pub struct NewCallArgsV2 {
    /// Chain id, according to the EIP-115 / ethereum-lists spec.
    pub chain_id: RawU256,
    /// Account which can upgrade this contract.
    /// Use empty to disable updatability.
    pub owner_id: AccountId,
    /// How many blocks after staging upgrade can deploy it.
    pub upgrade_delay_blocks: u64,
}
```

**File:** engine-workspace/src/lib.rs (L107-127)
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

        Ok(engine)
```

**File:** engine/src/lib.rs (L76-82)
```rust
    #[unsafe(no_mangle)]
    pub extern "C" fn new() {
        let io = Runtime;
        let env = Runtime;
        contract_methods::admin::new(io, &env)
            .map_err(ContractError::msg)
            .sdk_unwrap();
```

**File:** engine/src/xcc.rs (L120-130)
```rust
            promise_actions.push(PromiseAction::CreateAccount);
            promise_actions.push(PromiseAction::Transfer {
                amount: fund_amount,
            });
            promise_actions.push(PromiseAction::DeployContract { code });
            promise_actions.push(PromiseAction::FunctionCall {
                name: "initialize".into(),
                args: init_args.into_bytes(),
                attached_yocto: ZERO_YOCTO,
                gas: INITIALIZE_GAS,
            });
```
