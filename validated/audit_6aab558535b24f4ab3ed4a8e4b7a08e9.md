### Title
Single-Step Ownership Transfer with No Confirmation Permanently Locks Admin Control - (File: `engine/src/contract_methods/admin.rs`)

### Summary
The `set_owner` function in the Aurora Engine contract immediately overwrites `state.owner_id` with the caller-supplied `new_owner` in a single atomic step, with no confirmation required from the new owner. If the current owner supplies a syntactically valid but uncontrolled NEAR account ID (e.g., a typo), ownership is irrecoverably transferred to an account nobody controls. Because every emergency-response capability (`pause_contract`, `resume_contract`, `upgrade`, `stage_upgrade`, `attach_full_access_key`) is gated exclusively behind `require_owner_only`, the contract becomes permanently unupgradeable and unpauseable, removing all circuit-breaker protection over user funds.

### Finding Description
`set_owner` in `engine/src/contract_methods/admin.rs` performs a one-shot ownership transfer:

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

        state.owner_id = args.new_owner;   // ← immediate, irrevocable
        state::set_state(&mut io, &state)?;
        Ok(())
    })
}
``` [1](#0-0) 

The only guard is `ERR_SAME_OWNER` — there is no check that the new owner account actually exists on NEAR, no pending-acceptance step, and no way to revert the change after the transaction is finalized. [2](#0-1) 

`require_owner_only` compares `state.owner_id` against `predecessor_account_id` only; once `owner_id` is set to an uncontrolled account, no other path in the contract can override it:

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
``` [3](#0-2) 

Every critical admin function is gated behind this same check:

| Function | Effect if owner is lost |
|---|---|
| `pause_contract` | Contract can never be paused in an emergency |
| `resume_contract` | Contract can never be unpaused |
| `upgrade` / `stage_upgrade` | Contract can never be patched |
| `attach_full_access_key` | No full-access key can be added to recover |
| `set_eth_connector_contract_account` | Bridge connector address is frozen |
| `set_key_manager` | Relayer key management is frozen | [4](#0-3) [5](#0-4) [6](#0-5) 

The `EngineState` struct confirms `owner_id` is the sole authority field; there is no secondary admin or guardian role: [7](#0-6) 

### Impact Explanation
If `set_owner` is called with a syntactically valid but uncontrolled NEAR account ID, the Aurora Engine contract permanently loses its only emergency-response mechanism. The contract holds bridged ETH and ERC-20 token balances for all Aurora users. Without the ability to call `pause_contract` or `upgrade`, any subsequently discovered critical vulnerability (re-entrancy, accounting bug, precompile exploit) cannot be mitigated: the contract cannot be halted and cannot be patched. This constitutes **permanent freezing of funds** for all assets held in the engine at the time such a vulnerability is exploited, with no on-chain recovery path.

### Likelihood Explanation
NEAR account IDs are arbitrary human-readable UTF-8 strings (e.g., `aurora.near`, `dao.sputnik-v2.near`). Unlike Ethereum addresses, they carry no checksum and are easy to mistype. The owner account is typically a DAO or multisig whose ID may be long and complex. A single character transposition (e.g., `aurora-dao.near` vs `auroa-dao.near`) produces a syntactically valid but non-existent account ID that passes all current validation. The operation is performed manually by a privileged operator, making human error a realistic trigger.

### Recommendation
Implement a two-step ownership transfer:

1. **Step 1 — `propose_owner(new_owner: AccountId)`**: callable only by the current owner; stores `pending_owner_id` in state without changing `owner_id`.
2. **Step 2 — `accept_ownership()`**: callable only by the account whose ID matches `pending_owner_id`; atomically moves `pending_owner_id` into `owner_id`.

This ensures the new owner account demonstrably exists and is controlled before the transfer is finalized, matching the pattern already used by NEAR's own governance contracts.

### Proof of Concept

1. Current owner (`alice.near`) calls `set_owner` with `SetOwnerArgs { new_owner: "alce.near" }` (one-character typo).
2. `require_owner_only` passes (caller is `alice.near == state.owner_id`). `ERR_SAME_OWNER` does not trigger (`"alce.near" != "alice.near"`).
3. `state.owner_id` is set to `"alce.near"` and persisted via `state::set_state`.
4. `alice.near` attempts to call `pause_contract` in response to a discovered exploit — receives `ERR_NOT_ALLOWED`.
5. No account can call `set_owner` to recover, because only `"alce.near"` (which nobody controls) satisfies `require_owner_only`.
6. The exploit proceeds unmitigated; all bridged funds in the engine are permanently at risk with no on-chain remedy. [8](#0-7) [9](#0-8)

### Citations

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

**File:** engine/src/contract_methods/admin.rs (L250-272)
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

#[named]
pub fn resume_contract<I: IO + Copy, E: Env>(io: I, env: &E) -> Result<(), ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        let mut state = state::get_state(&io)?;
        require_owner_only(&state, &env.predecessor_account_id())?;
        require_paused(&state)?;
        state.is_paused = false;
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
