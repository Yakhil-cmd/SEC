### Title
Single-Step Ownership Transfer in `set_owner` Permanently Bricks Admin Functions — (File: `engine/src/contract_methods/admin.rs`)

### Summary
The `set_owner` function immediately replaces `state.owner_id` with the caller-supplied `new_owner` in a single atomic step, with no confirmation from the new owner. A typo or incorrect NEAR account ID permanently locks all `require_owner_only`-gated functions, including `resume_contract`, making a paused engine's fund freeze irreversible.

### Finding Description
In `engine/src/contract_methods/admin.rs`, the `set_owner` function reads `SetOwnerArgs { new_owner: AccountId }` from input and unconditionally writes it to the engine state:

```rust
state.owner_id = args.new_owner;
state::set_state(&mut io, &state)?;
```

The only guard is that the new owner must differ from the current owner (`ERR_SAME_OWNER`). There is no two-step handoff, no `pending_owner` storage slot, and no acceptance call. The `AccountId` type validates format (e.g., `"alice.near"`) but not on-chain existence or caller control. If the admin supplies a mistyped or nonexistent NEAR account ID, `owner_id` is permanently set to an uncontrollable account.

The `owner_id` field gates every critical administrative function:

- `resume_contract` — the only way to unpause the engine
- `upgrade` / `stage_upgrade` — the only way to deploy security patches
- `attach_full_access_key` — the only recovery mechanism
- `set_key_manager`, `pause_contract`, `resume_precompiles`, `set_eth_connector_contract_account` [1](#0-0) [2](#0-1) [3](#0-2) 

### Impact Explanation
**High — Permanent freezing of funds.**

The `owner_id` is the sole key to `resume_contract`. When `state.is_paused = true`, every state-mutating engine call (EVM execution, withdrawals, ERC-20 operations) is blocked by `require_running`. If ownership is transferred to an uncontrollable account while the contract is paused, `resume_contract` can never succeed, permanently freezing all bridged ETH and ERC-20 tokens held in the Aurora engine. There is no fallback: `start_hashchain` requires `require_key_manager_only` (a separate role), and `upgrade` also requires `require_owner_only`, so no code patch can be deployed either. [4](#0-3) [5](#0-4) 

### Likelihood Explanation
**Low** — requires an admin error (typo or copy-paste mistake) when supplying the new owner's NEAR account ID. NEAR account IDs are human-readable strings (e.g., `aurora`, `dao.aurora-treasury.near`), making a plausible typo realistic. The likelihood matches the original report exactly.

### Recommendation
Implement a two-step ownership transfer pattern:

1. Add a `pending_owner: Option<AccountId>` field to `EngineState`.
2. `set_owner` stores the new owner in `pending_owner` only.
3. Add an `accept_ownership` function that requires `env.predecessor_account_id() == state.pending_owner` and then promotes it to `owner_id`.

This ensures the new owner can only take effect after proving they control the account. [6](#0-5) 

### Proof of Concept
1. Current owner calls `pause_contract` → `state.is_paused = true`. All EVM execution and withdrawals are now blocked.
2. Current owner calls `set_owner` with `new_owner = "aurrora.near"` (one-character typo of the intended `"aurora.near"`).
3. `set_owner` passes the `ERR_SAME_OWNER` check (different string) and immediately writes `state.owner_id = "aurrora.near"`.
4. `resume_contract` checks `require_owner_only(&state, &env.predecessor_account_id())` — only `"aurrora.near"` can pass.
5. Since no one controls `"aurrora.near"`, `resume_contract` can never be called.
6. `upgrade` is also gated by `require_owner_only` — no code patch can be deployed.
7. All bridged ETH and ERC-20 tokens held in the Aurora engine are permanently frozen with no recovery path. [7](#0-6) [4](#0-3) [8](#0-7)

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

**File:** engine/src/contract_methods/admin.rs (L169-176)
```rust
pub fn upgrade<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<(), ContractError> {
    let state = state::get_state(&io)?;
    require_running(&state)?;
    require_owner_only(&state, &env.predecessor_account_id())?;
```

**File:** engine/src/contract_methods/admin.rs (L262-272)
```rust
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

**File:** engine/src/contract_methods/mod.rs (L65-70)
```rust
pub fn require_running(state: &state::EngineState) -> Result<(), ContractError> {
    if state.is_paused {
        return Err(errors::ERR_PAUSED.into());
    }
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
