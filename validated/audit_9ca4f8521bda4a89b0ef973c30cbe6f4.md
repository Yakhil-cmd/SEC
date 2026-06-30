### Title
`exit_to_near_precompile_callback` Reverts When Contract Is Paused, Causing Unwrapped NEAR Tokens to Be Frozen — (`engine/src/contract_methods/connector.rs`)

### Summary

The `exit_to_near_precompile_callback` function enforces a `require_running` guard at its entry point. When a user initiates a wNEAR-unwrap exit (burning wNEAR ERC-20 tokens and scheduling a `near_withdraw` promise), the NEAR tokens land in Aurora's account before the callback fires. If the contract is paused between the base promise and the callback, the callback panics, the NEAR tokens remain locked in Aurora's account with no automatic recovery path, and the user's ERC-20 tokens are already burned.

### Finding Description

The wNEAR-unwrap exit flow in `ExitToNear` precompile schedules two sequential NEAR actions:

1. `near_withdraw` on the wNEAR NEP-141 contract (burns wNEAR, credits NEAR to Aurora's account).
2. A callback to `exit_to_near_precompile_callback` that transfers those NEAR tokens to the user. [1](#0-0) 

The callback is registered unconditionally whenever `transfer_near_args` is `Some`: [2](#0-1) 

Inside the callback, the very first substantive check is `require_running`: [3](#0-2) 

If the Aurora Engine contract is paused (via `pause_contract`, a legitimate owner-only action), `require_running` returns an error, `sdk_unwrap()` panics, and the NEAR runtime reverts all state changes made *inside the callback*. However, the effects of the already-executed `near_withdraw` base promise are **not** reverted — the NEAR tokens are already in Aurora's account. [4](#0-3) 

The `pause_contract` function is a legitimate, owner-accessible feature: [5](#0-4) 

There is no retry or recovery mechanism for a failed callback in NEAR. Once the callback panics, the NEAR tokens remain in Aurora's account indefinitely, inaccessible to the user.

### Impact Explanation

**High — Temporary (potentially permanent) freezing of user funds.**

The user's wNEAR ERC-20 tokens are burned at EVM execution time (before any promise is scheduled). The NEAR tokens unwrapped by `near_withdraw` land in Aurora's NEAR account. If the callback fails, those NEAR tokens are stranded with no on-chain recovery path. The user loses both their ERC-20 tokens and the corresponding NEAR value until (and unless) an admin manually intervenes. Because NEAR callbacks are one-shot events, unpausing the contract does not automatically retry the transfer.

### Likelihood Explanation

**Medium.** Contract pausing is a documented, legitimate administrative feature of Aurora Engine. It can be triggered at any time for security or upgrade reasons. The window between `near_withdraw` completing and the callback executing is a single NEAR block, but given that pausing can happen at any time and the wNEAR unwrap path is a common user action, the probability of overlap is non-negligible. No attacker action is required — a routine admin pause is sufficient.

### Recommendation

Remove or relax the `require_running` guard inside `exit_to_near_precompile_callback`. This callback is a system-internal receipt that completes an already-initiated user action; it should not be subject to the same pause gate as user-facing entry points. A targeted fix:

```rust
pub fn exit_to_near_precompile_callback<...>(...) -> Result<Option<SubmitResult>, ContractError> {
    with_hashchain(io, env, function_name!(), |io| {
        let state = state::get_state(&io)?;
        // Do NOT call require_running here — this callback must always complete
        // to avoid stranding NEAR tokens already unwrapped by near_withdraw.
        env.assert_private_call()?;
        ...
    })
}
```

Alternatively, if the pause must be respected, the `near_withdraw` base promise should only be scheduled after confirming the contract will remain running, or a separate recovery function should be provided to allow users to reclaim stranded NEAR tokens.

### Proof of Concept

1. User holds wNEAR ERC-20 tokens on Aurora and calls `withdrawToNear` with the `:unwrap` suffix.
2. The EVM burns the ERC-20 tokens and the `ExitToNear` precompile emits a promise log encoding: `near_withdraw` → `exit_to_near_precompile_callback`.
3. `near_withdraw` executes in NEAR block N: wNEAR is burned on the wNEAR contract, and the equivalent NEAR yocto amount is credited to Aurora's account.
4. The Aurora contract owner calls `pause_contract` (a routine admin action) in block N or N+1.
5. `exit_to_near_precompile_callback` fires in block N+1. `require_running` returns `Err` because `state.is_paused == true`. `sdk_unwrap()` panics.
6. The callback's state changes are reverted. The NEAR tokens remain in Aurora's account. The user's ERC-20 tokens are already burned and cannot be recovered on-chain. [6](#0-5)

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

**File:** engine-precompiles/src/native.rs (L585-609)
```rust
    let (nep141_account_id, args, method, transfer_near_args, event) = match exit_params.message {
        // wNEAR address should be set via the `factory_set_wnear_address` transaction first.
        Some(Message::UnwrapWnear) if erc20_address == get_wnear_address(io).raw() =>
        // The flow is following here:
        // 1. We call `near_withdraw` on wNEAR account id on `aurora` behalf.
        // In such way we unwrap wNEAR to NEAR.
        // 2. After that, we call callback `exit_to_near_precompile_callback` on the `aurora`
        // in which make transfer of unwrapped NEAR to the `target_account_id`.
        {
            (
                nep141_account_id,
                format!(r#"{{"amount":"{}"}}"#, exit_params.amount.as_u128()),
                "near_withdraw",
                Some(TransferNearArgs {
                    target_account_id: exit_params.receiver_account_id.clone(),
                    amount: exit_params.amount.as_u128(),
                }),
                events::ExitToNear::Legacy(ExitToNearLegacy {
                    sender: Address::new(erc20_address),
                    erc20_address: Address::new(erc20_address),
                    dest: exit_params.receiver_account_id.to_string(),
                    amount: exit_params.amount,
                }),
            )
        }
```

**File:** engine/src/contract_methods/connector.rs (L196-244)
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
```

**File:** engine/src/lib.rs (L647-655)
```rust
    #[unsafe(no_mangle)]
    pub extern "C" fn exit_to_near_precompile_callback() {
        let io = Runtime;
        let env = Runtime;
        let mut handler = Runtime;
        contract_methods::connector::exit_to_near_precompile_callback(io, &env, &mut handler)
            .map_err(ContractError::msg)
            .sdk_unwrap();
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
