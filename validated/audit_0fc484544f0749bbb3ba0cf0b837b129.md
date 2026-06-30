### Title
Unprotected `new()` Initialization Allows Ownership Takeover of the Aurora Engine — (File: `engine/src/contract_methods/admin.rs`)

---

### Summary

The Aurora Engine's `new()` initialization function has no caller authentication. Any NEAR account can call it before the legitimate deployer does, setting themselves as the `owner_id` and gaining full administrative control over the engine contract, including the ability to upgrade contract code, add full-access keys, and pause the engine — all of which enable direct theft of user funds.

---

### Finding Description

The `new()` function is the sole initialization entrypoint for the Aurora Engine NEAR contract. It is exposed as a public `extern "C"` symbol via `lib.rs`: [1](#0-0) 

Its implementation in `admin.rs` contains only a single guard — a check that the state has not already been written: [2](#0-1) 

Specifically, lines 57–58 are the only protection:

```rust
if state::get_state(&io).is_ok() {
    return Err(b"ERR_ALREADY_INITIALIZED".into());
}
```

There is **no check** that `env::predecessor_account_id()` equals the contract account, the deployer, or any other authorized identity. The caller-supplied `NewCallArgs` (deserialized from input) determines the `owner_id` stored in engine state: [3](#0-2) 

In NEAR Protocol, deploying a contract and calling its initialization function are two distinct actions. While they *can* be batched atomically, if `new()` is called in a separate transaction — which is the common deployment pattern — any NEAR account monitoring the network can race to call `new()` first with attacker-controlled `NewCallArgs`, setting their own account as `owner_id`.

---

### Impact Explanation

**Critical — Direct theft of user funds and permanent loss of contract control.**

The `owner_id` stored by `new()` is the gatekeeper for every privileged operation in the engine:

- `upgrade()` — deploys arbitrary new contract code to the engine account, enabling complete logic replacement and fund theft.
- `attach_full_access_key()` — adds a full-access key to the engine's NEAR account, giving the attacker unrestricted account control.
- `pause_contract()` / `resume_contract()` — can permanently freeze all EVM execution.
- `set_owner()` — transfers ownership further, making recovery impossible. [4](#0-3) [5](#0-4) 

An attacker who wins the race to call `new()` effectively owns the entire Aurora EVM environment and all assets bridged through it.

---

### Likelihood Explanation

NEAR transactions are publicly observable. The window between contract deployment (which does not call `new()`) and the legitimate `new()` call is a race condition any NEAR account can exploit by submitting a competing transaction with higher priority. No special privileges, leaked keys, or social engineering are required — only the ability to submit a NEAR transaction.

---

### Recommendation

Enforce that `new()` can only be called by the contract account itself (i.e., `env::predecessor_account_id() == env::current_account_id()`), which is only satisfiable when the call is included in the same deployment batch transaction. Alternatively, require the deployment and `new()` call to always be submitted as a single atomic NEAR batch action, and add an explicit caller check inside `new()` to enforce this invariant at the contract level:

```rust
pub fn new<I: IO + Copy, E: Env>(mut io: I, env: &E) -> Result<(), ContractError> {
    if state::get_state(&io).is_ok() {
        return Err(b"ERR_ALREADY_INITIALIZED".into());
    }
    // Add: only the contract itself may initialize (enforces atomic deploy+init batch)
    if env.predecessor_account_id() != env.current_account_id() {
        return Err(b"ERR_UNAUTHORIZED".into());
    }
    // ... rest of init
}
```

---

### Proof of Concept

1. Attacker monitors the NEAR network for a deployment of the Aurora Engine WASM bytecode to a new account.
2. Before the deployer's `new()` transaction is included, the attacker submits their own call to `new()` on the same account, with `NewCallArgs` specifying `owner_id = attacker_account`.
3. The check `state::get_state(&io).is_ok()` returns `Err` (state is empty), so the guard passes.
4. `state::set_state` writes the attacker's `EngineState` with `owner_id = attacker_account`.
5. The legitimate deployer's `new()` call now fails with `ERR_ALREADY_INITIALIZED`.
6. The attacker calls `upgrade()` to deploy a malicious contract, or `attach_full_access_key()` to take full control of the NEAR account, draining all bridged assets. [2](#0-1) [1](#0-0)

### Citations

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
