### Title
Uninitialized Aurora Engine Contract Allows Any Caller to Seize Ownership via `new` - (File: engine/src/contract_methods/admin.rs)

### Summary
The Aurora Engine's `new` initialization function imposes no caller restriction and is invoked as a separate transaction from contract deployment. Any NEAR account can call `new` on the Aurora Engine before the legitimate deployer does, setting an attacker-controlled `owner_id` and gaining full administrative control over the engine.

### Finding Description
The `new` function in `engine/src/contract_methods/admin.rs` guards against re-initialization only by checking whether state already exists:

```rust
pub fn new<I: IO + Copy, E: Env>(mut io: I, env: &E) -> Result<(), ContractError> {
    if state::get_state(&io).is_ok() {
        return Err(b"ERR_ALREADY_INITIALIZED".into());
    }
    ...
    state::set_state(&mut io, &state)?;
    Ok(())
}
``` [1](#0-0) 

There is no check on `env.predecessor_account_id()`. Any NEAR account can call `new` on the Aurora Engine contract if it has not yet been initialized.

In the workspace deployment helper, contract deployment and initialization are issued as two separate transactions:

```rust
let contract = account.deploy(&self.code...).await?;
...
engine.new(self.chain_id, self.owner_id, self.upgrade_delay_blocks)
      .transact().await
``` [2](#0-1) 

This creates a window between the `DeployContract` action and the `new` call during which any NEAR account can submit a `new` call with attacker-controlled arguments (including `owner_id`).

This is the direct analog of the reported UUPS uninitialized implementation contract pattern: the initialization function is publicly callable, has no caller restriction, and is not atomically bound to deployment.

### Impact Explanation
If an attacker's `new` call is included before the legitimate deployer's call, the attacker becomes the `owner_id` stored in `EngineState`. As owner, the attacker can:

- Call `upgrade` / `stage_upgrade` to deploy a malicious WASM contract that drains all ETH and ERC-20 balances held by the engine.
- Call `pause_contract` to freeze the engine, permanently blocking withdrawals.
- Call `set_owner` to transfer ownership to another account, locking out the legitimate team.
- Call `attach_full_access_key` to add a full-access key to the engine account.

All of these are gated solely on `require_owner_only`: [3](#0-2) [4](#0-3) 

Impact: **Critical** — direct path to theft of all user funds via malicious upgrade, or permanent fund freeze via pause.

### Likelihood Explanation
Low-to-medium. The attack window is the gap between the `DeployContract` action and the `new` function call transaction. On NEAR, these are separate receipts and can land in different blocks. An attacker monitoring the NEAR mempool or block explorer for a new Aurora Engine deployment can submit a `new` call with their own `owner_id` in the same or next block. The `NewCallArgs` format (chain_id, owner_id, upgrade_delay_blocks) is public and trivially constructable. The attack is fully permissionless — no special keys or privileges are required to call a public function on any NEAR contract.

### Recommendation
Call `new` in the same NEAR batch transaction as `DeployContract`, so initialization is atomic with deployment. This is the same pattern already used correctly for the XCC Router: [5](#0-4) 

For the main engine, the deployment batch should be:
```
[DeployContract { code }, FunctionCall { name: "new", args: <NewCallArgs> }]
```
This eliminates the window entirely. Additionally, consider adding a check that `env.predecessor_account_id() == env.current_account_id()` inside `new`, so only a self-call (from a batch) is accepted.

### Proof of Concept
1. Observe a new Aurora Engine WASM being deployed to a NEAR account (e.g., via block explorer or RPC subscription).
2. Immediately submit a NEAR transaction calling `new` on that account with:
   - `owner_id` = attacker's NEAR account
   - `chain_id` = any valid value
   - `upgrade_delay_blocks` = 0
3. If the attacker's transaction is included before the legitimate deployer's `new` call, `state::get_state` returns `Ok` for all subsequent calls, and the attacker's `owner_id` is permanently stored.
4. The legitimate deployer's `new` call returns `ERR_ALREADY_INITIALIZED`.
5. The attacker calls `upgrade` with a malicious WASM that transfers all ETH balances to the attacker's EVM address. [1](#0-0) [6](#0-5)

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

**File:** engine/src/contract_methods/admin.rs (L104-121)
```rust
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

**File:** engine-workspace/src/lib.rs (L95-128)
```rust
    pub async fn deploy_and_init(self) -> anyhow::Result<EngineContract> {
        let owner_id = self.owner_id.as_ref();
        let (owner, root) = owner_id.split_once('.').unwrap_or((owner_id, owner_id));
        let node = Node::new(root, self.root_balance).await?;
        let account = if owner == root {
            node.root()
        } else {
            node.root()
                .create_subaccount(owner, self.contract_balance)
                .await?
        };
        let public_key = account.public_key()?;
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
    }
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
