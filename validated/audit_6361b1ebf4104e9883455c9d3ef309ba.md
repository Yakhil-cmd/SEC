### Title
Single-Step Owner Transfer Allows Irrecoverable Loss of Engine Control - (File: engine/src/contract_methods/admin.rs)

### Summary
The `set_owner` function in Aurora Engine performs an immediate, single-step transfer of the `owner_id` role with no confirmation from the new owner and no pending-transfer state. A typo, address-poisoning attack, or accidental call can permanently transfer the most privileged role in the system to an unintended account, with no recovery path.

### Finding Description
`set_owner` reads a `new_owner` account ID from calldata and writes it directly into `EngineState` in a single atomic step:

```rust
// engine/src/contract_methods/admin.rs, lines 103-121
pub fn set_owner<I: IO + Copy, E: Env>(io: I, env: &E) -> Result<(), ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        let mut state = state::get_state(&io)?;
        require_running(&state)?;
        require_owner_only(&state, &env.predecessor_account_id())?;

        let args: SetOwnerArgs = io.read_input_borsh()?;
        if state.owner_id == args.new_owner {
            return Err(errors::ERR_SAME_OWNER.into());
        }

        state.owner_id = args.new_owner;   // ← immediate, unconditional write
        state::set_state(&mut io, &state)?;
        Ok(())
    })
}
``` [1](#0-0) 

The only guard is `require_owner_only`, which checks that the *current* owner is the caller. [2](#0-1) 

There is no:
- pending-transfer slot that the new owner must accept,
- cancellation window,
- zero-address / existence check on `new_owner`, or
- separate "renounce" path.

The `SetOwnerArgs` struct carries only a single `new_owner: AccountId` field, confirming no two-step data is tracked anywhere. [3](#0-2) 

### Impact Explanation

The `owner_id` is the master key for the entire engine. Functions exclusively gated behind `require_owner_only` include:

| Function | Consequence if owner is wrong/attacker |
|---|---|
| `upgrade` | Deploy arbitrary new WASM → drain all ETH/ERC-20 balances |
| `attach_full_access_key` | Add a full-access key to the NEAR account → complete account takeover |
| `stage_upgrade` | Stage malicious code for deployment |
| `pause_contract` / `resume_contract` | Freeze or unfreeze the engine at will |
| `set_upgrade_delay_blocks` | Set delay to 0, enabling instant upgrades |
| `set_key_manager` | Hijack relayer key management | [4](#0-3) [5](#0-4) 

**Scenario A — Transfer to attacker-controlled account:** An attacker registers a NEAR account with a name visually similar to the intended recipient (e.g., `aurora-dao.near` vs `aurora_dao.near`). The owner calls `set_owner` with the wrong ID. The attacker immediately calls `upgrade` with malicious WASM or `attach_full_access_key` to obtain a full-access key, enabling direct theft of all user funds held in the engine. **Impact: Critical — direct theft of user funds.**

**Scenario B — Transfer to non-existent or inaccessible account:** The owner makes a typo (e.g., `auroradao.nea` instead of `auroradao.near`). NEAR account IDs are validated for format but not for existence at the contract level. Ownership is permanently lost. The engine can never be upgraded, resumed from a pause, or have its connector reconfigured. **Impact: Critical — permanent freezing of funds.**

### Likelihood Explanation

- NEAR account IDs are arbitrary UTF-8 strings; typos and lookalike accounts are a realistic operational hazard.
- The `set_owner` call is a routine administrative operation with no friction or confirmation step.
- No on-chain existence check is performed on `new_owner` before the write.
- The `attach_full_access_key` capability makes exploitation of a successful transfer immediately catastrophic, incentivizing targeted address-poisoning attacks against the owner.

Likelihood: **Medium** (operational mistake) to **High** (targeted address-poisoning).

### Recommendation

Implement a two-step push/pull transfer pattern:

1. **Push (`propose_owner`):** The current owner writes a `pending_owner: Option<AccountId>` field into `EngineState`. The existing `owner_id` is unchanged.
2. **Pull (`accept_owner`):** Only the account stored in `pending_owner` may call this function; it atomically sets `owner_id = pending_owner` and clears `pending_owner`.
3. **Cancel (`cancel_owner_transfer`):** The current owner can clear `pending_owner` at any time.

This ensures the new owner must prove they control the account before the transfer is finalized, eliminating both typo and address-poisoning risks.

### Proof of Concept

```
1. Owner account: `real-owner.near`
2. Attacker registers: `real-0wner.near` (zero instead of 'o')
3. Attacker waits for the owner to initiate a transfer to `real-owner2.near`
4. Via social engineering or a front-end substitution, the owner submits:
       set_owner({ new_owner: "real-0wner.near" })
5. `set_owner` passes `require_owner_only` (caller is `real-owner.near`) ✓
6. `state.owner_id` is immediately set to `real-0wner.near` ✓
7. Attacker (controlling `real-0wner.near`) calls:
       attach_full_access_key({ public_key: <attacker_key> })
   — passes `require_owner_only` because `real-0wner.near == state.owner_id` ✓
8. Attacker now holds a full-access key on the Aurora Engine NEAR account.
9. Attacker calls `upgrade` with WASM that transfers all ETH balances to attacker address.
   All user funds are drained.
```

Root cause line: [6](#0-5)

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

**File:** engine-types/src/parameters/engine.rs (L117-122)
```rust
/// Borsh-encoded parameters for the `set_owner` function.
#[derive(Debug, Clone, PartialEq, Eq, BorshSerialize, BorshDeserialize)]
#[cfg_attr(feature = "impl-serde", derive(Serialize, Deserialize))]
pub struct SetOwnerArgs {
    pub new_owner: AccountId,
}
```
