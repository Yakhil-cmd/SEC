### Title
Pause Contract Flag Not Enforced in EVM Transaction and Connector Execution Paths — (`engine/src/contract_methods/evm_transactions.rs`, `engine/src/contract_methods/connector.rs`)

---

### Summary

The Aurora Engine exposes a `pause_contract` admin function that sets `state.is_paused = true` in `EngineState`. A `require_running` guard exists to enforce this flag. However, the primary user-facing execution paths — EVM transaction submission (`submit`, `submit_with_args`, `call`, `deploy_code`) and ETH connector operations (`ft_on_transfer`, `withdraw`, `ft_transfer`, `ft_transfer_call`) — never invoke `require_running`. As a result, pausing the contract has no effect on any user-initiated EVM or bridging activity.

---

### Finding Description

`pause_contract` in `engine/src/contract_methods/admin.rs` writes `is_paused = true` into the persisted `EngineState`:

```rust
// engine/src/contract_methods/admin.rs ~line 256
state.is_paused = true;
state::set_state(&mut io, &state)?;
```

The enforcement helper `require_running` in `engine/src/contract_methods/mod.rs` reads this flag:

```rust
// engine/src/contract_methods/mod.rs lines 65-70
pub fn require_running(state: &state::EngineState) -> Result<(), ContractError> {
    if state.is_paused {
        return Err(errors::ERR_PAUSED.into());
    }
    Ok(())
}
```

In `engine/src/lib.rs`, the contract entry points for EVM execution and the ETH connector delegate directly to their implementation modules **without** calling `require_running` or `require_owner_and_running` first:

```rust
// lib.rs ~line 275 — no require_running call
pub extern "C" fn submit() {
    contract_methods::evm_transactions::submit(io, &env, &mut handler)...
}

// lib.rs ~line 603 — no require_running call
pub extern "C" fn ft_on_transfer() {
    contract_methods::connector::ft_on_transfer(io, &env, &mut handler)...
}
```

A grep across all `engine/**/*.rs` files for `is_paused`, `paused`, `require_running`, or `pause_contract` returns **zero matches** in `engine/src/contract_methods/evm_transactions.rs` and `engine/src/contract_methods/connector.rs`. This confirms neither module checks the pause flag internally.

By contrast, `deploy_upgrade` in `lib.rs` (lines 175–178) does correctly call `require_running` before proceeding, demonstrating the pattern is known but inconsistently applied.

The only existing test for `pause_contract` (`engine-tests/src/tests/pause_contract.rs`) verifies only that the function requires the owner — it never asserts that pausing actually blocks `submit`, `call`, `ft_on_transfer`, or `withdraw`.

---

### Impact Explanation

**Critical — Direct theft of user funds / bypass of emergency stop.**

The `pause_contract` mechanism is the engine's primary emergency brake. If an active exploit is draining bridged ETH or ERC-20 tokens, the owner would call `pause_contract` expecting all user-initiated operations to halt. Because `submit`, `submit_with_args`, `call`, `deploy_code`, `ft_on_transfer`, `withdraw`, `ft_transfer`, and `ft_transfer_call` never check `is_paused`, the exploit continues unimpeded. Funds that the pause was intended to protect remain fully accessible to the attacker.

---

### Likelihood Explanation

**High.** The pause mechanism is explicitly implemented and documented as a safety control. Any security incident that prompts the owner to invoke `pause_contract` will expose this gap. The attacker needs only to continue sending normal EVM transactions or NEP-141 transfers — no special knowledge or privilege is required. The entry paths (`submit`, `ft_on_transfer`) are the standard interfaces used by every Aurora user and bridge relayer.

---

### Recommendation

Add a `require_running` check at the start of every user-facing mutative entry point. At minimum:

- `contract_methods::evm_transactions::submit`
- `contract_methods::evm_transactions::submit_with_args`
- `contract_methods::evm_transactions::call`
- `contract_methods::evm_transactions::deploy_code`
- `contract_methods::connector::ft_on_transfer`
- `contract_methods::connector::withdraw`
- `contract_methods::connector::ft_transfer`
- `contract_methods::connector::ft_transfer_call`

Each should load the engine state and call `require_running(&state)?` before any further logic, mirroring the pattern already used in `deploy_upgrade` and all admin functions.

---

### Proof of Concept

1. Owner calls `pause_contract` (authenticated, succeeds). `EngineState.is_paused` is now `true`.
2. Attacker submits a signed Ethereum transaction via `submit` targeting a vulnerable EVM contract.
3. `lib.rs::submit` delegates to `evm_transactions::submit` with no pause check.
4. The EVM transaction executes normally; funds are transferred to the attacker.
5. The pause flag had no observable effect on the attack path. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** engine/src/contract_methods/admin.rs (L250-259)
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

**File:** engine/src/lib.rs (L174-185)
```rust
        let mut io = Runtime;
        let state = state::get_state(&io).sdk_unwrap();
        require_running(&state)
            .map_err(ContractError::msg)
            .sdk_unwrap();
        let index = internal_get_upgrade_index();
        if io.block_height() <= index {
            sdk::panic_utf8(errors::ERR_NOT_ALLOWED_TOO_EARLY);
        }
        Runtime::self_deploy(&bytes_to_key(KeyPrefix::Config, CODE_KEY));
        io.remove_storage(&bytes_to_key(KeyPrefix::Config, CODE_STAGE_KEY));
    }
```

**File:** engine/src/lib.rs (L274-282)
```rust
    #[unsafe(no_mangle)]
    pub extern "C" fn submit() {
        let io = Runtime;
        let env = Runtime;
        let mut handler = Runtime;
        contract_methods::evm_transactions::submit(io, &env, &mut handler)
            .map_err(ContractError::msg)
            .sdk_unwrap();
    }
```

**File:** engine/src/lib.rs (L602-610)
```rust
    #[unsafe(no_mangle)]
    pub extern "C" fn ft_on_transfer() {
        let io = Runtime;
        let env = Runtime;
        let mut handler = Runtime;
        contract_methods::connector::ft_on_transfer(io, &env, &mut handler)
            .map_err(ContractError::msg)
            .sdk_unwrap();
    }
```

**File:** engine/src/state.rs (L27-29)
```rust
    /// Flag to pause and unpause the engine.
    pub is_paused: bool,
    /// Relayer key manager.
```
