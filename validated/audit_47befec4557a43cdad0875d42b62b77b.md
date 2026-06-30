### Title
Single-Step Owner Transfer Allows Permanent Loss of Contract Control - (File: `engine/src/contract_methods/admin.rs`)

### Summary
The `set_owner` function in Aurora Engine transfers `owner_id` in a single atomic step with no confirmation from the new owner. If the current owner supplies a wrong or inaccessible NEAR account ID, the contract permanently loses its owner, making it impossible to upgrade, pause, or perform other critical administrative operations — potentially leading to permanent freezing of user funds.

### Finding Description
`set_owner` at `engine/src/contract_methods/admin.rs:104-121` directly overwrites `state.owner_id` with the caller-supplied `new_owner` in one transaction:

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
        state.owner_id = args.new_owner;   // ← immediate, irreversible
        state::set_state(&mut io, &state)?;
        Ok(())
    })
}
``` [1](#0-0) 

There is no pending-owner slot, no acceptance step, and no recovery path. The only guard is `require_owner_only`, which confirms the *caller* is the current owner — it does nothing to validate that the *new* owner address is reachable. [2](#0-1) 

The `owner_id` field in `EngineState` is the sole gatekeeper for every critical administrative function: [3](#0-2) 

Functions exclusively gated by `require_owner_only`:

| Function | Effect |
|---|---|
| `upgrade` | Deploy arbitrary new contract bytecode |
| `stage_upgrade` | Stage new code for deployment |
| `set_upgrade_delay_blocks` | Change upgrade delay |
| `pause_contract` / `resume_contract` | Emergency pause/unpause |
| `resume_precompiles` | Resume paused precompiles |
| `set_key_manager` | Change relayer key manager |
| `attach_full_access_key` | Add full access keys to the contract |
| `set_eth_connector_contract_account` | Redirect the ETH connector |
| `factory_update` | Update XCC router bytecode | [4](#0-3) 

The `SetOwnerArgs` struct accepts any `AccountId` string with no existence check: [5](#0-4) 

NEAR account IDs are human-readable strings (e.g., `"aurora.near"`), making typographical errors realistic and undetectable before the transaction is finalized.

### Impact Explanation
**Critical — Permanent freezing of funds.**

If `owner_id` is set to a non-existent or inaccessible NEAR account:

1. **No upgrade path**: `upgrade` and `stage_upgrade` are permanently inaccessible. If a critical bug is discovered in the EVM execution, bridge accounting, or connector logic, there is no mechanism to deploy a fix. All user funds held in the Aurora EVM or the ETH connector become permanently frozen.
2. **No emergency pause**: `pause_contract` cannot be called. An ongoing exploit cannot be stopped.
3. **No ETH connector redirect**: `set_eth_connector_contract_account` cannot be called, so a compromised connector cannot be replaced.

The Aurora Engine holds bridged ETH and NEP-141 token balances for all Aurora users. Loss of the upgrade path is equivalent to permanent loss of the ability to protect those funds.

### Likelihood Explanation
**Low-Medium.**

- The operation is performed by the current owner, a trusted party.
- However, NEAR account IDs are arbitrary strings. A single character typo (e.g., `"auroa.near"` instead of `"aurora.near"`, or a staging account ID pasted by mistake) produces a silently accepted but permanently wrong owner.
- The only existing guard (`ERR_SAME_OWNER`) does not catch this case.
- The operation is irreversible: once `set_state` is called, the old owner has no recourse.
- There is no on-chain confirmation event or time-lock that would allow detection and cancellation before the change takes effect.

### Recommendation
Implement a two-step ownership transfer pattern:

1. **Propose**: Current owner calls `propose_owner(new_owner)`, which stores `new_owner` as `pending_owner_id` in `EngineState` without changing `owner_id`.
2. **Accept**: The proposed new owner calls `accept_ownership()`, which verifies `predecessor_account_id == pending_owner_id` and then sets `owner_id = pending_owner_id`, clearing `pending_owner_id`.

This guarantees the new owner address is valid and accessible before the transfer is finalized, eliminating the risk of permanent ownership loss due to a typo or copy-paste error.

### Proof of Concept

1. Current owner (e.g., `"aurora.near"`) calls `set_owner` with `new_owner = "auroa.near"` (one-character typo).
2. `require_owner_only` passes — caller is the current owner.
3. `state.owner_id != args.new_owner` check passes — different string.
4. `state.owner_id` is set to `"auroa.near"` and persisted via `set_state`.
5. `"auroa.near"` does not exist on NEAR mainnet and is not controlled by anyone.
6. All subsequent calls to `upgrade`, `pause_contract`, `resume_contract`, `set_eth_connector_contract_account`, etc. revert with `ERR_NOT_ALLOWED`.
7. A critical bug discovered in the EVM or bridge logic cannot be patched. All user funds in the Aurora EVM and ETH connector are permanently frozen.

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

**File:** engine/src/state.rs (L19-31)
```rust
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

**File:** engine-types/src/parameters/engine.rs (L117-122)
```rust
/// Borsh-encoded parameters for the `set_owner` function.
#[derive(Debug, Clone, PartialEq, Eq, BorshSerialize, BorshDeserialize)]
#[cfg_attr(feature = "impl-serde", derive(Serialize, Deserialize))]
pub struct SetOwnerArgs {
    pub new_owner: AccountId,
}
```
