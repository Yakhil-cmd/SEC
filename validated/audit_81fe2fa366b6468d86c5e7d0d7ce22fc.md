### Title
Unprotected `new()` Initialization Allows Front-Running to Seize Engine Ownership and Drain Funds - (File: `engine/src/contract_methods/admin.rs`)

---

### Summary

The Aurora Engine's `new()` initialization function performs no check on `env.predecessor_account_id()`. Any NEAR account can call it before the legitimate deployer, set an arbitrary `owner_id`, and then use the owner-gated `attach_full_access_key()` to add a full access key to the Aurora Engine NEAR account — granting complete control over the contract and all funds it holds.

---

### Finding Description

The `new()` function in `engine/src/contract_methods/admin.rs` is the sole initialization entry point for the Aurora Engine. Its only guard is a re-initialization check:

```rust
pub fn new<I: IO + Copy, E: Env>(mut io: I, env: &E) -> Result<(), ContractError> {
    if state::get_state(&io).is_ok() {
        return Err(b"ERR_ALREADY_INITIALIZED".into());
    }
    // ...
    state::set_state(&mut io, &state)?;
    Ok(())
}
```

There is no validation that `env.predecessor_account_id()` equals `env.current_account_id()` (self-call) or any other trusted deployer address. The `owner_id` written into `EngineState` is taken entirely from caller-supplied `NewCallArgs`:

```rust
let args = NewCallArgs::deserialize(&input)...;
let state: EngineState = args.into();
state::set_state(&mut io, &state)?;
```

The `EngineState.owner_id` field is the sole gatekeeper for all privileged operations, including `attach_full_access_key`, `upgrade`, `stage_upgrade`, `set_owner`, `pause_contract`, and `set_upgrade_delay_blocks`.

The deployment flow in the workspace confirms that deployment and initialization are **two separate transactions**:

```rust
let contract = account.deploy(&self.code...).await?;
// ...
engine.new(self.chain_id, self.owner_id, self.upgrade_delay_blocks).transact().await...
```

This creates a window between the deploy transaction and the `new()` transaction during which any NEAR account can call `new()` first.

Contrast this with the XCC router's `initialize()`, which correctly handles the first-caller-becomes-parent pattern atomically within a NEAR batch:

```rust
match parent.get() {
    None => { parent.set(&caller); }
    Some(parent) => {
        if (caller != parent) && (caller != env::current_account_id()) {
            env::panic_str(ERR_ILLEGAL_CALLER);
        }
    }
}
```

The Aurora Engine's `new()` has no equivalent protection.

---

### Impact Explanation

**Critical — Direct theft of all user funds.**

An attacker who successfully front-runs `new()` becomes the `owner_id`. They can immediately call `attach_full_access_key()`:

```rust
pub fn attach_full_access_key<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I, env: &E, handler: &mut H,
) -> Result<(), ContractError> {
    let state = state::get_state(&io)?;
    require_running(&state)?;
    require_owner_only(&state, &env.predecessor_account_id())?;
    // ...
    let action = PromiseAction::AddFullAccessKey { public_key, nonce: 0 };
```

A full access key on the Aurora Engine NEAR account (`aurora`) gives the attacker unrestricted ability to: transfer all NEAR held by the engine, deploy arbitrary replacement contract code, delete the account, and drain all bridged ETH and ERC-20 token balances. The entire TVL of the Aurora Engine is at risk.

---

### Likelihood Explanation

**High.** On NEAR Protocol, contract deployment and initialization are routinely separate transactions. An attacker monitoring the NEAR network (via RPC or indexer) can observe the deploy transaction in a block and submit a `new()` call in the same or next block before the deployer's initialization transaction is included. No special privileges, leaked keys, or social engineering are required — only a standard NEAR account and knowledge of the `NewCallArgs` Borsh encoding.

---

### Recommendation

Restrict `new()` to be callable only by the contract account itself (i.e., require `predecessor_account_id == current_account_id`), or deploy and initialize atomically in a single NEAR batch transaction. The minimal fix:

```rust
pub fn new<I: IO + Copy, E: Env>(mut io: I, env: &E) -> Result<(), ContractError> {
    if state::get_state(&io).is_ok() {
        return Err(b"ERR_ALREADY_INITIALIZED".into());
    }
    // Add: only the contract itself (via a batch deploy+init) may initialize
    if env.predecessor_account_id() != env.current_account_id() {
        return Err(b"ERR_NOT_ALLOWED".into());
    }
    // ...
}
```

Alternatively, the deployment tooling must always combine `DeployContract` and the `new()` function call into a single atomic NEAR batch action, as is done for the XCC router.

---

### Proof of Concept

1. Deployer broadcasts a transaction deploying `aurora-engine.wasm` to account `aurora`.
2. Attacker observes the deploy transaction on-chain.
3. Attacker calls `aurora.new({"chain_id": ..., "owner_id": "attacker.near", "upgrade_delay_blocks": 0})`.
4. Deployer's `new()` call arrives and fails with `ERR_ALREADY_INITIALIZED`.
5. Attacker calls `aurora.attach_full_access_key({"public_key": "<attacker_key>"})` as `attacker.near` (the now-registered owner).
6. Attacker's key is added as a full access key to the `aurora` account.
7. Attacker uses the full access key to transfer all NEAR, redeploy arbitrary code, or delete the account — draining all user funds.

**Root cause**: [1](#0-0) 

**Privileged operations gated solely on `owner_id`**: [2](#0-1) 

**`owner_id` is caller-supplied with no validation**: [3](#0-2) 

**Separate deploy and init transactions confirm the window**: [4](#0-3) 

**XCC router's correct atomic initialization pattern (for contrast)**: [5](#0-4)

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

**File:** etc/xcc-router/src/lib.rs (L76-89)
```rust
        let caller = env::predecessor_account_id();
        let mut parent = LazyOption::new(StorageKey::Parent, None);
        match parent.get() {
            None => {
                parent.set(&caller);
            }
            Some(parent) => {
                // Allow self-calls to `initialize` also.
                // This happens during the upgrade flow.
                if (caller != parent) && (caller != env::current_account_id()) {
                    env::panic_str(ERR_ILLEGAL_CALLER);
                }
            }
        }
```
