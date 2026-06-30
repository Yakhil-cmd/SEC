### Title
Missing Error-Handling Callback on NEAR Transfer in wNEAR Unwrap Flow Causes Permanent Fund Freeze - (File: engine/src/contract_methods/connector.rs)

### Summary
In the wNEAR-to-NEAR unwrap exit path, `exit_to_near_precompile_callback` schedules a NEAR transfer to the user-specified target account after a successful `near_withdraw`, but attaches **no error-handling callback** to that transfer promise. If the NEAR transfer fails (e.g., the target account does not exist on NEAR), the user's wNEAR ERC-20 tokens are permanently burned and the unwrapped NEAR is permanently stranded in the Aurora engine account with no on-chain recovery path.

### Finding Description
The `ExitToNear` precompile supports a wNEAR unwrap path triggered when a user burns wNEAR ERC-20 tokens with the `:unwrap` suffix. The flow is:

1. The EVM execution burns the user's wNEAR ERC-20 balance (EVM state updated immediately and irrevocably).
2. The precompile schedules `near_withdraw` on the wNEAR NEP-141 contract, with `exit_to_near_precompile_callback` as the callback.
3. In `exit_to_near_precompile_callback`, if `near_withdraw` succeeded and `transfer_near` args are present, a `PromiseAction::Transfer` is created to send the unwrapped NEAR to `args.target_account_id`.
4. **No error-handling callback is attached to this NEAR transfer promise.** [1](#0-0) 

The critical gap: when `near_withdraw` succeeds, the callback creates a NEAR transfer and calls `handler.promise_return(promise_id)`, returning `None` for the EVM result. If the NEAR transfer subsequently fails, the callback itself fails silently. The ERC-20 tokens burned in step 1 are gone, and the NEAR sits in the Aurora engine account with no on-chain mechanism to return it to the user. [2](#0-1) 

The `transfer_near_args` are populated for the wNEAR unwrap case (`Message::UnwrapWnear`), setting `target_account_id` to the user-supplied recipient. The user fully controls this value. [3](#0-2) 

Contrast with the `near_withdraw` failure path: when `near_withdraw` fails and the `error_refund` feature is enabled, `refund_on_error` is called to re-mint the burned ERC-20 tokens. No equivalent recovery exists for the NEAR transfer failure. [4](#0-3) [5](#0-4) 

This is structurally analogous to the reported `withdrawPmx()` bug: a withdrawal operation (`near_withdraw`) successfully removes assets from one layer, but the subsequent state update (NEAR delivery to user) has no error path, leaving the system in an irrecoverable inconsistent state — the EVM-side accounting (ERC-20 burned) and the NEAR-side outcome (NEAR not delivered) are permanently mismatched.

### Impact Explanation
- The user's wNEAR ERC-20 tokens are permanently burned with no refund.
- The equivalent NEAR is permanently stranded in the Aurora engine account.
- The user suffers a total, unrecoverable loss of their bridged wNEAR funds.
- **Impact: Permanent freezing of user funds (High).**

### Likelihood Explanation
- Any unprivileged EVM user can trigger this by calling the wNEAR ERC-20 `burn` function with the `:unwrap` suffix and specifying a non-existent NEAR account as the recipient.
- In NEAR, a `Transfer` action to a non-existent account fails at the protocol level.
- Realistic triggers: mistyped account IDs, deleted accounts, or accounts that have not yet been created on NEAR mainnet.
- **Likelihood: Medium** — requires a user-controlled input error, but the contract provides no guard or recovery.

### Recommendation
Attach an error-handling callback to the NEAR transfer promise inside `exit_to_near_precompile_callback`. If the NEAR transfer fails, the callback should invoke `refund_on_error` (or an equivalent) to re-mint the burned wNEAR ERC-20 tokens to the user's refund address — mirroring the existing recovery logic already used for `near_withdraw` failures when `error_refund` is enabled.

### Proof of Concept
1. Alice holds wNEAR ERC-20 tokens on Aurora.
2. Alice calls the wNEAR ERC-20 `burn` function with the `:unwrap` suffix, specifying `nonexistent.near` as the target account.
3. The `ExitToNear` precompile burns Alice's wNEAR ERC-20 balance (EVM state updated, irreversible at this point).
4. `near_withdraw` is called on the wNEAR NEP-141 contract; it succeeds — NEAR is now held in the Aurora engine account.
5. `exit_to_near_precompile_callback` is invoked; it schedules a `PromiseAction::Transfer` to `nonexistent.near` with no error callback attached.
6. The NEAR transfer fails because `nonexistent.near` does not exist on NEAR.
7. Alice's wNEAR ERC-20 tokens are permanently burned; the NEAR is permanently stranded in the Aurora engine account.
8. Alice has lost her funds with no on-chain recovery path.

### Citations

**File:** engine/src/contract_methods/connector.rs (L214-230)
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

            None
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

**File:** engine/src/engine.rs (L1176-1224)
```rust
pub fn refund_on_error<I: IO + Copy, E: Env, P: PromiseHandler>(
    io: I,
    env: &E,
    state: EngineState,
    args: &RefundCallArgs,
    handler: &mut P,
) -> EngineResult<SubmitResult> {
    let current_account_id = env.current_account_id();
    if let Some(erc20_address) = args.erc20_address {
        // ERC-20 exit; re-mint burned tokens
        let erc20_admin_address = current_address(&current_account_id);
        let mut engine: Engine<_, _> =
            Engine::new_with_state(state, erc20_admin_address, current_account_id, io, env);

        let refund_address = args.recipient_address;
        let amount = U256::from_big_endian(&args.amount);
        let input = setup_refund_on_error_input(amount, refund_address);

        engine.call(
            &erc20_admin_address,
            &erc20_address,
            Wei::zero(),
            input,
            u64::MAX,
            Vec::new(),
            Vec::new(),
            handler,
        )
    } else {
        // ETH exit; transfer ETH back from precompile address
        let exit_address = exit_to_near::ADDRESS;
        let mut engine: Engine<_, _> =
            Engine::new_with_state(state, exit_address, current_account_id, io, env);
        let refund_address = args.recipient_address;
        let amount = Wei::new(U256::from_big_endian(&args.amount));
        engine.call(
            &exit_address,
            &refund_address,
            amount,
            Vec::new(),
            u64::MAX,
            vec![
                (exit_address.raw(), Vec::new()),
                (refund_address.raw(), Vec::new()),
            ],
            Vec::new(),
            handler,
        )
    }
```
