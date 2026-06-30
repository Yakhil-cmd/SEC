### Title
Uninitialized Aurora Engine Contract Allows Any Caller to Seize Ownership and Steal All Bridged Funds - (File: `engine/src/contract_methods/admin.rs`)

### Summary
The Aurora Engine's `new()` initialization function imposes no caller restriction. If the engine WASM is deployed without atomically calling `new()` in the same NEAR batch transaction, any unprivileged NEAR account can front-run the initialization, set itself as `owner_id`, and subsequently steal all bridged ETH/ERC-20 tokens or permanently freeze the protocol.

### Finding Description
The `new()` function in `engine/src/contract_methods/admin.rs` only checks whether state already exists:

```rust
pub fn new<I: IO + Copy, E: Env>(mut io: I, env: &E) -> Result<(), ContractError> {
    if state::get_state(&io).is_ok() {
        return Err(b"ERR_ALREADY_INITIALIZED".into());
    }
    // ...
    state::set_state(&mut io, &state)?;
    Ok(())
}
``` [1](#0-0) 

There is no check that `env.predecessor_account_id()` equals the contract account or any authorized deployer. The `owner_id` and `upgrade_delay_blocks` fields of `EngineState` are set entirely from attacker-controlled input bytes. [2](#0-1) 

The public WASM entry point exposes this with no additional guard:

```rust
#[unsafe(no_mangle)]
pub extern "C" fn new() {
    let io = Runtime;
    let env = Runtime;
    contract_methods::admin::new(io, &env)
        .map_err(ContractError::msg)
        .sdk_unwrap();
}
``` [3](#0-2) 

The workspace test utility demonstrates the two-transaction deployment pattern (deploy, then separately call `new()`), confirming the window exists in practice:

```rust
let contract = account.deploy(&self.code...).await?;
// ...
engine.new(self.chain_id, self.owner_id, self.upgrade_delay_blocks)
    .transact().await...
``` [4](#0-3) 

### Impact Explanation
**Critical — direct theft of all user funds.**

Once the attacker holds `owner_id`, they can:

1. Call `attach_full_access_key` to add a full-access key to the engine NEAR account, gaining unrestricted control over the account and all its NEAR balance.
2. Call `stage_upgrade` with malicious WASM, then `upgrade` to replace the engine with code that mints or transfers all bridged ETH/ERC-20 tokens to the attacker.
3. Call `set_owner` to permanently lock out the legitimate deployer. [5](#0-4) 

Because the attacker supplies `upgrade_delay_blocks = 0` in the `new()` call, the upgrade can be executed immediately with no waiting period.

### Likelihood Explanation
**Low-Medium.** The vulnerability requires a window between the `DeployContract` action and the `new()` call. NEAR supports atomic batch transactions that could close this window, but the reference deployment utility uses two separate transactions, and no on-chain enforcement prevents a two-step deployment. A monitoring attacker watching the NEAR mempool or block explorer for a freshly deployed but uninitialized `aurora` contract account can exploit this in a single subsequent transaction.

### Recommendation
- **Short-term**: Add a caller restriction to `new()`: require `env.predecessor_account_id() == env.current_account_id()`. This forces initialization to be a self-call, which is only possible inside a batch transaction that also deploys the contract.
- **Long-term**: Enforce that deployment and initialization are always performed atomically in a single NEAR batch transaction (`DeployContract` + `FunctionCall("new")` in one receipt), mirroring the pattern already used for XCC router deployment. [6](#0-5) 

### Proof of Concept

1. Attacker watches the NEAR blockchain for the Aurora Engine contract account (`aurora`) to receive a new WASM deployment without a subsequent `new()` call in the same block.
2. Attacker immediately submits a transaction calling `new` on the `aurora` contract with:
   - `owner_id = attacker.near`
   - `upgrade_delay_blocks = 0`
   - Any valid `chain_id`
3. `state::get_state` returns `Err(NotFound)`, so the guard passes. `EngineState { owner_id: attacker.near, upgrade_delay_blocks: 0, ... }` is written to storage.
4. Attacker calls `stage_upgrade` with malicious WASM that transfers all ETH balances to `attacker.near`.
5. Attacker calls `upgrade` (no delay because `upgrade_delay_blocks = 0`).
6. All bridged ETH and ERC-20 tokens held by the Aurora Engine are stolen. [1](#0-0) [7](#0-6)

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

**File:** engine/src/state.rs (L18-31)
```rust
#[derive(Default, Clone, PartialEq, Eq, Debug)]
pub struct EngineState {
    /// Chain id, according to the EIP-155 / ethereum-lists spec.
    pub chain_id: [u8; 32],
    /// Account which can upgrade this contract.
    /// Use empty to disable updatability.
    pub owner_id: AccountId,
    /// How many blocks after staging upgrade can deploy it.
    pub upgrade_delay_blocks: u64,
    /// Flag to pause and unpause the engine.
    pub is_paused: bool,
    /// Relayer key manager.
    pub key_manager: Option<AccountId>,
}
```

**File:** engine/src/state.rs (L207-214)
```rust
/// Gets the state from storage, if it exists otherwise it will error.
pub fn get_state<I: IO + Copy>(io: &I) -> Result<EngineState, EngineStateError> {
    io.read_storage(&bytes_to_key(KeyPrefix::Config, STATE_KEY))
        .map_or_else(
            || Err(EngineStateError::NotFound),
            |bytes| EngineState::try_from_slice(&bytes.to_vec(), io),
        )
}
```

**File:** engine/src/lib.rs (L76-83)
```rust
    #[unsafe(no_mangle)]
    pub extern "C" fn new() {
        let io = Runtime;
        let env = Runtime;
        contract_methods::admin::new(io, &env)
            .map_err(ContractError::msg)
            .sdk_unwrap();
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

**File:** engine/src/xcc.rs (L226-237)
```rust
            if *create_needed {
                promise_actions.push(PromiseAction::CreateAccount);
                promise_actions.push(PromiseAction::Transfer {
                    amount: STORAGE_AMOUNT,
                });
                promise_actions.push(PromiseAction::DeployContract { code });
                promise_actions.push(PromiseAction::FunctionCall {
                    name: "initialize".into(),
                    args: init_args.into_bytes(),
                    attached_yocto: ZERO_YOCTO,
                    gas: INITIALIZE_GAS,
                });
```
