### Title
ERC-20 Tokens Permanently Burned When Contract Is Paused During `exit_to_near_precompile_callback` - (File: engine/src/contract_methods/connector.rs)

---

### Summary

When the Aurora Engine contract is paused between an exit-to-near EVM transaction (which burns ERC-20 tokens) and its NEAR callback, the `exit_to_near_precompile_callback` function fails at the `require_running` guard before reaching the token-refund logic. The burned ERC-20 tokens are permanently unrecoverable.

---

### Finding Description

The exit-to-near flow works as follows:

1. A user submits an EVM transaction calling the exit-to-near precompile for an ERC-20 token.
2. The ERC-20 contract burns the tokens (EVM state committed).
3. The engine schedules a NEAR promise: `ft_transfer_call` on the NEP-141 contract.
4. The engine attaches `exit_to_near_precompile_callback` as the callback of that promise.

The callback handles two outcomes of `ft_transfer_call`:
- **Success**: optionally transfer NEAR to the user.
- **Failure**: call `engine::refund_on_error` to re-mint the burned ERC-20 tokens.

The critical problem is that `exit_to_near_precompile_callback` calls `require_running` **before** either branch is reached:

```rust
// engine/src/contract_methods/connector.rs
pub fn exit_to_near_precompile_callback<...>(...) -> Result<Option<SubmitResult>, ContractError> {
    with_hashchain(io, env, function_name!(), |io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;          // ← blocks everything if paused
        env.assert_private_call()?;
        ...
        let maybe_result = if let Some(PromiseResult::Successful(_)) = handler.promise_result(0) {
            // transfer NEAR
        } else if let Some(args) = args.refund {
            let refund_result = engine::refund_on_error(...)?;  // ← never reached
            ...
        };
    })
}
```

If the owner pauses the contract (for any legitimate operational reason — security incident, upgrade, etc.) between step 2 and step 4, the callback transaction panics at `require_running`. The NEAR runtime marks the callback as failed. The ERC-20 tokens are already burned in EVM state and there is no mechanism to re-trigger the callback or recover the tokens after resuming.

The `require_running` guard is defined in `engine/src/contract_methods/mod.rs`:

```rust
pub fn require_running(state: &state::EngineState) -> Result<(), ContractError> {
    if state.is_paused {
        return Err(errors::ERR_PAUSED.into());
    }
    Ok(())
}
```

The pause is set by `pause_contract` in `engine/src/contract_methods/admin.rs`:

```rust
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

The contract can be resumed, but resuming does not help: the callback promise has already failed and cannot be re-executed. The burned ERC-20 tokens remain permanently unrecoverable.

---

### Impact Explanation

**Critical — Permanent freezing of funds.**

Any user who initiates an exit-to-near ERC-20 withdrawal while the contract is subsequently paused (before the callback fires) loses their tokens permanently. The EVM burn is committed in the original transaction; the refund path inside `exit_to_near_precompile_callback` is unreachable when `is_paused = true`. Resuming the contract does not restore the lost tokens because NEAR promise callbacks cannot be replayed.

---

### Likelihood Explanation

**Low-Medium.** The owner must pause the contract during the narrow window between the exit-to-near EVM transaction and its NEAR callback (typically one or two NEAR blocks). Pausing for security incidents, emergency upgrades, or operational maintenance is a documented and expected use of `pause_contract`. Any such pause while exit-to-near transactions are in-flight triggers the loss. The more active the bridge usage, the higher the probability of overlap.

---

### Recommendation

Move the `require_running` check **after** the refund branch, or restructure `exit_to_near_precompile_callback` so that the refund path executes unconditionally when `ft_transfer_call` has failed, regardless of the contract's pause state. A minimal fix:

```rust
pub fn exit_to_near_precompile_callback<...>(...) {
    with_hashchain(io, env, function_name!(), |io| {
        let state = state::get_state(&io)?;
        env.assert_private_call()?;
        if handler.promise_results_count() != 1 {
            return Err(errors::ERR_PROMISE_COUNT.into());
        }
        let args: ExitToNearPrecompileCallbackArgs = io.read_input_borsh()?;

        // Always attempt refund if ft_transfer_call failed, even when paused
        if handler.promise_result(0).is_none() {
            if let Some(refund_args) = args.refund {
                engine::refund_on_error(io, env, state, &refund_args, handler)?;
                return Ok(None);
            }
        }

        // Only gate the success-path (NEAR transfer) behind require_running
        require_running(&state)?;
        ...
    })
}
```

---

### Proof of Concept

**Attacker-controlled entry path:**

1. User calls `submit` (or `call`) on the Aurora Engine with an EVM transaction that invokes the exit-to-near precompile for an ERC-20 token. This is a standard, unprivileged user action.
2. The EVM executes: the ERC-20 contract burns the user's tokens; the engine schedules `ft_transfer_call` + `exit_to_near_precompile_callback`.
3. The owner calls `pause_contract` (legitimate operational action, e.g., for an upgrade or security response).
4. `ft_transfer_call` fires on the NEP-141 contract and fails (e.g., recipient not registered, NEP-141 contract paused, or any other failure).
5. `exit_to_near_precompile_callback` fires on the Aurora Engine. `require_running` returns `ERR_PAUSED`. The callback panics. `refund_on_error` is never called.
6. The ERC-20 tokens remain burned. The user has no recourse even after the contract is resumed.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** engine/src/contract_methods/connector.rs (L196-246)
```rust
pub fn exit_to_near_precompile_callback<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<Option<SubmitResult>, ContractError> {
    with_hashchain(io, env, function_name!(), |io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;
        env.assert_private_call()?;

        // This function should only be called as the callback of
        // exactly one promise.
        if handler.promise_results_count() != 1 {
            return Err(errors::ERR_PROMISE_COUNT.into());
        }

        let args: ExitToNearPrecompileCallbackArgs = io.read_input_borsh()?;

        let maybe_result = if let Some(PromiseResult::Successful(_)) = handler.promise_result(0) {
            if let Some(args) = args.transfer_near {
                let action = PromiseAction::Transfer {
                    amount: Yocto::new(args.amount),
                };
                let promise = PromiseBatchAction {
                    target_account_id: args.target_account_id,
                    actions: vec![action],
                };

                // Safety: this call is safe because it comes from the exit to near precompile, not users.
                // The call is to transfer the unwrapped wNEAR tokens.
                let promise_id = handler.promise_create_batch(&promise);
                handler.promise_return(promise_id);
            }

            None
        } else if let Some(args) = args.refund {
            // Exit call failed; need to refund tokens
            let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;

            if !refund_result.status.is_ok() {
                return Err(errors::ERR_REFUND_FAILURE.into());
            }

            Some(refund_result)
        } else {
            None
        };

        Ok(maybe_result)
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
