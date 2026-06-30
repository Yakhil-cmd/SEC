### Title
`require_running` in `exit_to_near_precompile_callback` Permanently Freezes User Funds When Contract Is Paused During In-Flight Exit - (File: engine/src/contract_methods/connector.rs)

### Summary
When the Aurora Engine contract is paused while an `exitToNear` cross-contract call is in-flight and the downstream `ft_transfer` promise fails, the `exit_to_near_precompile_callback` function cannot execute its refund branch because `require_running` blocks it. The user's ERC-20 tokens are already burned in the EVM, and the NEP-141 tokens remain permanently stuck in Aurora with no recovery path.

### Finding Description

The `exitToNear` precompile flow is a two-step cross-contract operation:

1. **Step 1 — `submit()`:** The user submits an EVM transaction calling the `exitToNear` precompile. The EVM executes, burning the user's ERC-20 tokens. The precompile schedules a `ft_transfer` promise on the NEP-141 contract.

2. **Step 2 — `exit_to_near_precompile_callback`:** After the `ft_transfer` promise resolves (success or failure), this callback fires. If the `ft_transfer` failed, the callback's refund branch calls `engine::refund_on_error` to re-mint the ERC-20 tokens.

The callback is defined as:

```rust
// engine/src/contract_methods/connector.rs:196-246
pub fn exit_to_near_precompile_callback<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I, env: &E, handler: &mut H,
) -> Result<Option<SubmitResult>, ContractError> {
    with_hashchain(io, env, function_name!(), |io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;          // <-- BLOCKS WHEN PAUSED
        env.assert_private_call()?;
        ...
        } else if let Some(args) = args.refund {
            // Exit call failed; need to refund tokens
            let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;
            ...
        }
    })
}
```

`require_running` is defined as:

```rust
// engine/src/contract_methods/mod.rs:65-70
pub fn require_running(state: &state::EngineState) -> Result<(), ContractError> {
    if state.is_paused {
        return Err(errors::ERR_PAUSED.into());
    }
    Ok(())
}
```

If the contract owner calls `pause_contract` between Step 1 and Step 2 (a realistic maintenance window spanning multiple NEAR blocks), the callback returns `Err(ERR_PAUSED)` immediately. The refund branch never executes. The ERC-20 burn from Step 1 is irreversible, and the NEP-141 tokens remain in Aurora's custody with no mechanism to recover them.

The same `require_running` guard also blocks the `withdraw` function:

```rust
// engine/src/contract_methods/connector.rs:43-59
pub fn withdraw<I: IO + Copy + PromiseHandler, E: Env>(
    io: I, env: &E,
) -> Result<(), ContractError> {
    require_running(&state::get_state(&io)?)?;   // blocks all user withdrawals when paused
    ...
}
```

This causes a secondary temporary freeze of all NEP-141 ETH withdrawals to Ethereum while the contract is paused.

### Impact Explanation

**Primary (Permanent Freeze):** When `exit_to_near_precompile_callback` fires while the contract is paused and the `ft_transfer` promise has failed, the user's ERC-20 tokens are permanently destroyed with no refund. The NEP-141 tokens remain locked in Aurora's account. There is no recovery function available to unpause-and-retry the callback; the NEAR promise result is consumed and cannot be replayed.

**Secondary (Temporary Freeze):** All users holding NEP-141 ETH on NEAR are unable to call `withdraw` to bridge back to Ethereum for the entire duration of the pause.

Both impacts fall within the allowed scope: permanent freezing of funds (critical) and temporary freezing of funds (high).

### Likelihood Explanation

**Medium.** The contract owner has a legitimate reason to call `pause_contract` (e.g., for upgrades, emergency response, or maintenance). NEAR cross-contract calls span at least one block boundary between the `submit()` call and the callback execution. Any pause issued in that window — even unintentionally — triggers the permanent freeze for any in-flight exit whose `ft_transfer` subsequently fails. The `ft_transfer` can fail for ordinary reasons (recipient account not registered with the NEP-141 contract), making the combination of events realistic.

### Recommendation

Remove `require_running(&state)?` from `exit_to_near_precompile_callback`. This callback is a private, system-internal function (`env.assert_private_call()` is enforced immediately after) and is only ever invoked as the scheduled callback of a promise created by the engine itself. It must be allowed to complete its refund logic regardless of the contract's pause state, exactly as the original report recommended removing `AppStorage.enforceExodusMode()` from `withdrawNFT()`.

Optionally, also remove `require_running` from `withdraw` or add an owner-bypass path, so that users can always exit their funds even during a maintenance pause.

### Proof of Concept

1. User calls `submit()` with an EVM transaction that calls the `exitToNear` precompile to bridge ERC-20 tokens to NEAR. ERC-20 tokens are burned in the EVM. A `ft_transfer` promise is scheduled on the NEP-141 contract.
2. Before the callback executes (next NEAR block), the contract owner calls `pause_contract`. `state.is_paused` is set to `true`.
3. The `ft_transfer` promise fails (e.g., recipient account not registered).
4. NEAR runtime invokes `exit_to_near_precompile_callback`. The function calls `require_running`, which returns `Err(ERR_PAUSED)`. The callback aborts.
5. The refund branch (`engine::refund_on_error`) never runs. The user's ERC-20 tokens are permanently gone. The NEP-141 tokens remain in Aurora's account with no recovery path. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** engine/src/contract_methods/connector.rs (L43-59)
```rust
pub fn withdraw<I: IO + Copy + PromiseHandler, E: Env>(
    io: I,
    env: &E,
) -> Result<(), ContractError> {
    require_running(&state::get_state(&io)?)?;
    env.assert_one_yocto()?;

    let args: WithdrawCallArgs = io.read_input_borsh()?;
    let args = borsh::to_vec(&EngineWithdrawCallArgs {
        sender_id: env.predecessor_account_id(),
        recipient_address: args.recipient_address,
        amount: args.amount,
    })
    .unwrap();

    return_promise(io, env, "engine_withdraw", args, ONE_YOCTO)
}
```

**File:** engine/src/contract_methods/connector.rs (L196-203)
```rust
pub fn exit_to_near_precompile_callback<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<Option<SubmitResult>, ContractError> {
    with_hashchain(io, env, function_name!(), |io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;
```

**File:** engine/src/contract_methods/connector.rs (L231-239)
```rust
        } else if let Some(args) = args.refund {
            // Exit call failed; need to refund tokens
            let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;

            if !refund_result.status.is_ok() {
                return Err(errors::ERR_REFUND_FAILURE.into());
            }

            Some(refund_result)
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

**File:** engine/src/contract_methods/admin.rs (L250-260)
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
```
