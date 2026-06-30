### Title
Permanent Fund Freeze in wNEAR Unwrap Flow When NEAR Transfer to Target Account Fails - (`engine/src/contract_methods/connector.rs`)

---

### Summary

When a user unwraps wNEAR ERC-20 tokens via the `ExitToNear` precompile with the `:unwrap` suffix, the `exit_to_near_precompile_callback` function directly pushes NEAR to the user-specified `target_account_id` using a fire-and-forget promise with no failure callback. If the NEAR transfer fails (e.g., the target NEAR account does not exist), the wNEAR tokens are already irreversibly burned and the unwrapped NEAR is permanently stranded inside the Aurora contract with no recovery path for the user.

---

### Finding Description

The wNEAR unwrap flow is initiated in `exit_erc20_token_to_near` inside `engine-precompiles/src/native.rs`. When the user calls `withdrawToNear` with the `:unwrap` suffix on the wNEAR ERC-20 contract, the precompile sets `transfer_near_args` pointing to the user-supplied `receiver_account_id`: [1](#0-0) 

This produces a two-promise chain:

1. **Base promise**: `near_withdraw` is called on the wNEAR NEP-141 contract, burning the wNEAR and releasing raw NEAR into the Aurora contract's balance.
2. **Callback**: `exit_to_near_precompile_callback` is invoked. [2](#0-1) 

Inside the callback, when the base `near_withdraw` promise succeeds, the code creates a NEAR `Transfer` batch promise targeting `args.target_account_id` and immediately returns it with `promise_return` — with **no further callback** to handle failure: [3](#0-2) 

The existing `refund` branch in the same callback only fires when `near_withdraw` itself fails (i.e., `PromiseResult` is not `Successful`): [4](#0-3) 

There is no analogous recovery path for the case where `near_withdraw` **succeeds** but the subsequent NEAR transfer to `target_account_id` **fails**. In NEAR protocol, a `Transfer` action to a non-existent named account fails and the NEAR is returned to the predecessor (the Aurora contract). However, the Aurora contract exposes no function for users to reclaim NEAR stranded this way, and the

### Citations

**File:** engine-precompiles/src/native.rs (L462-483)
```rust
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

**File:** engine-precompiles/src/native.rs (L587-608)
```rust
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
```

**File:** engine/src/contract_methods/connector.rs (L214-228)
```rust
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
