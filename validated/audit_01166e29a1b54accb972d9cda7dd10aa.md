### Title
Engine Pause Mid-Flight Permanently Freezes User Funds in `exit_to_near_precompile_callback` - (File: `engine/src/contract_methods/connector.rs`)

---

### Summary

The `exit_to_near_precompile_callback` function enforces `require_running` before executing either the NEAR transfer (wNEAR-unwrap success path) or the ERC-20 refund (error-refund failure path). Because NEAR cross-contract calls span multiple blocks, the engine owner can legitimately call `pause_contract` between the initial EVM transaction (which already burns the user's wNEAR/ERC-20 tokens) and the callback execution. When this happens, the callback panics on `require_running`, the NEAR transfer or token refund never executes, and the user's funds are permanently frozen.

---

### Finding Description

**Step 1 – User initiates wNEAR unwrap via ExitToNear precompile.**

A user submits an EVM transaction that calls the `ExitToNear` precompile with the `:unwrap` suffix. Inside `exit_erc20_token_to_near`, the wNEAR ERC-20 tokens are burned from the user's EVM balance, and a two-step NEAR promise is constructed:

- **Base promise**: `near_withdraw` on the wNEAR NEP-141 contract (unwraps wNEAR → NEAR, crediting the engine account).
- **Callback**: `exit_to_near_precompile_callback` on the engine itself, which is supposed to forward the unwrapped NEAR to the user. [1](#0-0) 

Because `transfer_near_args` is `Some(...)` for the wNEAR-unwrap path, `callback_args != default()`, so the callback promise is always attached. [2](#0-1) 

**Step 2 – Owner calls `pause_contract` between blocks.**

The engine owner calls `pause_contract`, which sets `state.is_paused = true`. This is a legitimate admin action (e.g., responding to an incident). [3](#0-2) 

**Step 3 – Callback fires and immediately fails.**

When `exit_to_near_precompile_callback` executes in a subsequent block, the very first substantive check is `require_running`: [4](#0-3) 

`require_running` returns `ERR_PAUSED` and the function returns an error before reaching either:

- The NEAR transfer to the user (lines 215–228), or
- The ERC-20 refund path (lines 231–239). [5](#0-4) 

**Result**: The user's wNEAR ERC-20 tokens were already burned in the EVM state during the original transaction. The NEAR that was unwrapped now sits in the engine account with no mechanism to recover it. NEAR callbacks are one-time events; once the callback fails, it cannot be re-executed even after the engine is resumed.

The same freeze applies to the ERC-20 error-refund path (when `feature = "error_refund"` is enabled): if the base `ft_transfer`/`ft_transfer_call` promise fails and the engine is paused, the refund re-mint never executes, and the user's burned ERC-20 tokens are permanently lost. [6](#0-5) 

---

### Impact Explanation

**Critical – Permanent freezing of funds.**

- User's wNEAR ERC-20 tokens are burned from EVM state (irreversible once the EVM transaction commits).
- The unwrapped NEAR is credited to the engine account but never forwarded to the user.
- After engine resumption, there is no replay mechanism for the failed callback; the NEAR remains permanently locked in the engine account.
- For the ERC-20 error-refund variant: burned ERC-20 tokens are never re-minted, constituting direct theft of user funds.

---

### Likelihood Explanation

**Medium.** The engine pause is a legitimate admin action that is most likely to be used during an emergency or incident response — precisely the scenario where in-flight cross-contract calls are most likely to be present. Any user who initiated a wNEAR unwrap in the blocks immediately before a pause will lose their funds. The window is narrow per-user but the impact per occurrence is total loss of the withdrawn amount.

---

### Recommendation

Remove the `require_running` guard from `exit_to_near_precompile_callback`, or scope it so that it does not block the NEAR-transfer and refund paths. This callback is a private, engine-internal settlement step (`env.assert_private_call()` already enforces this); it must be allowed to complete regardless of the engine's pause state, because the user's tokens were already consumed in the preceding transaction. A minimal fix is:

```rust
pub fn exit_to_near_precompile_callback<...>(...) -> Result<...> {
    with_hashchain(io, env, function_name!(), |io| {
        let state = state::get_state(&io)?;
        // REMOVED: require_running(&state)?;   <-- do not block settlement callbacks
        env.assert_private_call()?;
        ...
    })
}
```

---

### Proof of Concept

1. User calls `submit` with an EVM transaction that invokes `ExitToNear` on the wNEAR ERC-20 address with the `:unwrap` suffix and amount `A`.
2. EVM execution burns `A` wNEAR from the user's EVM balance. A NEAR promise chain is scheduled: `near_withdraw(A)` → callback `exit_to_near_precompile_callback`.
3. In the next NEAR block, `near_withdraw` executes successfully; `A` yoctoNEAR is now held by the engine account.
4. Before the callback executes, the engine owner calls `pause_contract`.
5. `exit_to_near_precompile_callback` fires. `require_running` returns `ERR_PAUSED`; the function panics.
6. The NEAR transfer `PromiseAction::Transfer { amount: A }` to the user is never created.
7. The user has lost `A` wNEAR (burned from EVM) and `A` NEAR (stuck in engine account) with no recovery path. [7](#0-6) [3](#0-2)

### Citations

**File:** engine-precompiles/src/native.rs (L449-483)
```rust
        let callback_args = ExitToNearPrecompileCallbackArgs {
            #[cfg(feature = "error_refund")]
            refund: refund_call_args(&exit_to_near_params, &exit_event),
            #[cfg(not(feature = "error_refund"))]
            refund: None,
            transfer_near: transfer_near_args,
        };
        let attached_gas = if method == "ft_transfer_call" {
            costs::FT_TRANSFER_CALL_GAS
        } else {
            costs::FT_TRANSFER_GAS
        };

        let transfer_promise = PromiseCreateArgs {
            target_account_id: nep141_address,
            method,
            args: args.into_bytes(),
            attached_balance: Yocto::new(1),
            attached_gas,
        };

        let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
            PromiseArgs::Create(transfer_promise)
        } else {
            PromiseArgs::Callback(PromiseWithCallbackArgs {
                base: transfer_promise,
                callback: PromiseCreateArgs {
                    target_account_id: self.current_account_id.clone(),
                    method: "exit_to_near_precompile_callback".to_string(),
                    args: borsh::to_vec(&callback_args).unwrap(),
                    attached_balance: Yocto::new(0),
                    attached_gas: costs::EXIT_TO_NEAR_CALLBACK_GAS,
                },
            })
        };
```

**File:** engine/src/contract_methods/admin.rs (L251-260)
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
