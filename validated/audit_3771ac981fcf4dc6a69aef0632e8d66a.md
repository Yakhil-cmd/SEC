The code is clear. Let me verify the `lib.rs` export for `start_hashchain` to confirm it's a public entrypoint.

### Title
`key_manager` Can Unconditionally Resume a Paused Contract via `start_hashchain`, Bypassing Owner-Only Authorization - (File: engine/src/contract_methods/admin.rs)

---

### Summary

`start_hashchain` is gated by `require_key_manager_only` but unconditionally sets `state.is_paused = false` before returning. Because `resume_contract` is owner-only, this gives the `key_manager` — a distinct, lower-privilege role — the ability to lift any owner-initiated pause without owner approval.

---

### Finding Description

`resume_contract` enforces `require_owner_only` before clearing the pause flag: [1](#0-0) 

`start_hashchain` enforces only `require_paused` + `require_key_manager_only`, then unconditionally clears the same flag: [2](#0-1) 

The `require_key_manager_only` guard confirms the caller matches `state.key_manager` but does not check `owner_id`: [3](#0-2) 

`key_manager` is a separate account set by the owner via `set_key_manager`, which requires `require_owner_only` + `require_running`: [4](#0-3) 

The `EngineState` struct stores both `owner_id` and `key_manager` as independent fields, confirming they are distinct roles: [5](#0-4) 

`start_hashchain` is a public NEAR contract method exported from `lib.rs`, making it reachable by any NEAR account that holds the `key_manager` identity.

---

### Impact Explanation

When the owner calls `pause_contract` to block fund movements (e.g., during an active exploit or emergency), the `key_manager` can immediately call `start_hashchain` with any valid `StartHashchainArgs` (block height ≤ current block, arbitrary `block_hashchain` bytes). This clears `is_paused`, restoring full engine operation — including EVM transactions, ETH withdrawals, and bridge exits — without the owner's knowledge or consent. The owner's intended freeze is silently lifted by a party that was never granted resume authority.

**Impact:** High — Temporary freezing of funds is lifted by an unauthorized party.

---

### Likelihood Explanation

Preconditions are both realistic and common in production Aurora deployments:

1. A `key_manager` is set (standard operational configuration for relayer key management).
2. The owner calls `pause_contract` (the intended emergency response path).
3. The `key_manager` calls `start_hashchain` — a publicly callable method with no additional barriers beyond valid Borsh-encoded args.

All three conditions are reachable through normal production contract calls with no infrastructure compromise required.

---

### Recommendation

Remove the `state.is_paused = false` assignment from `start_hashchain`, or replace it with a `require_owner_only` check before clearing the pause flag — consistent with `resume_contract`. If the hashchain initialization flow genuinely requires resuming the contract, it should be a two-step process: `key_manager` initializes the hashchain, then the owner explicitly calls `resume_contract`. [6](#0-5) 

---

### Proof of Concept

```
// Preconditions (set up in test harness):
// 1. Deploy engine, owner = "aurora"
// 2. Call set_key_manager(key_manager = "relayer_manager.near") as "aurora"
// 3. Call pause_contract() as "aurora"  →  state.is_paused == true

// Attack:
// 4. Call start_hashchain(StartHashchainArgs { block_height: N, block_hashchain: [0u8;32] })
//    as "relayer_manager.near"
//    - require_paused(&state)          → passes (is_paused == true)
//    - require_key_manager_only(...)   → passes (predecessor == key_manager)
//    - state.is_paused = false         ← contract resumed without owner
//    - state::set_state(...)           ← persisted

// Assert:
// 5. state.is_paused == false          ← owner's pause bypassed
// 6. submit() / call() now succeed     ← fund movements unblocked
```

### Citations

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

**File:** engine/src/contract_methods/admin.rs (L275-295)
```rust
pub fn set_key_manager<I: IO + Copy, E: Env>(io: I, env: &E) -> Result<(), ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        let mut state = state::get_state(&io)?;

        require_running(&state)?;
        require_owner_only(&state, &env.predecessor_account_id())?;

        let key_manager =
            serde_json::from_slice::<RelayerKeyManagerArgs>(&io.read_input().to_vec())
                .map(|args| args.key_manager)
                .map_err(|_| errors::ERR_JSON_DESERIALIZE)?;

        if state.key_manager == key_manager {
            return Err(errors::ERR_SAME_KEY_MANAGER.into());
        }

        state.key_manager = key_manager;
        state::set_state(&mut io, &state)?;

        Ok(())
    })
```

**File:** engine/src/contract_methods/admin.rs (L426-463)
```rust
pub fn start_hashchain<I: IO + Copy, E: Env>(mut io: I, env: &E) -> Result<(), ContractError> {
    let mut state = state::get_state(&io)?;
    require_paused(&state)?;
    require_key_manager_only(&state, &env.predecessor_account_id())?;

    let input = io.read_input().to_vec();
    let args = StartHashchainArgs::try_from_slice(&input).map_err(|_| errors::ERR_SERIALIZE)?;
    let block_height = env.block_height();

    // Starting hashchain must be for an earlier block
    if block_height < args.block_height {
        return Err(errors::ERR_ARGS.into());
    }

    let mut hashchain = Hashchain::new(
        state.chain_id,
        env.current_account_id(),
        args.block_height + 1,
        args.block_hashchain,
    );

    if hashchain.get_current_block_height() < block_height {
        hashchain.move_to_block(block_height)?;
    }

    hashchain.add_block_tx(
        block_height,
        function_name!(),
        &input,
        &[],
        &Bloom::default(),
    )?;
    crate::hashchain::save_hashchain(&mut io, &hashchain)?;

    state.is_paused = false;
    state::set_state(&mut io, &state)?;

    Ok(())
```

**File:** engine/src/contract_methods/mod.rs (L99-111)
```rust
fn require_key_manager_only(
    state: &state::EngineState,
    predecessor_account_id: &AccountId,
) -> Result<(), ContractError> {
    let key_manager = state
        .key_manager
        .as_ref()
        .ok_or(errors::ERR_KEY_MANAGER_IS_NOT_SET)?;
    if key_manager != predecessor_account_id {
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
