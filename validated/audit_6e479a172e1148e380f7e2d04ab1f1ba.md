### Title
ERC-20 Tokens Permanently Frozen When `ft_transfer_call` Partially Refunds in Omni Exit Path - (`engine-precompiles/src/native.rs`)

### Summary

The `ExitToNear` precompile's Omni path uses `ft_transfer_call` to transfer NEP-141 tokens to a receiver. Per the NEP-141 standard, the receiver's `ft_on_transfer` may return a non-zero `unused_amount`, causing the NEP-141 contract to refund those tokens back to Aurora. However, Aurora has no callback logic to re-mint the corresponding ERC-20 tokens to the user. The ERC-20 tokens were already burned atomically during EVM execution, so the refunded NEP-141 tokens become permanently frozen on Aurora's NEP-141 balance.

---

### Finding Description

When a user calls `exitToNear` on an ERC-20 contract with an Omni message, the flow is:

1. The ERC-20 contract calls `_burn(msg.sender, amount)` and then invokes the `ExitToNear` precompile.
2. The precompile constructs a `ft_transfer_call` promise targeting the NEP-141 contract.
3. The NEP-141 contract transfers `amount` from Aurora to the receiver, then calls `receiver.ft_on_transfer(aurora, amount, msg)`.
4. The receiver's `ft_on_transfer` may return `unused_amount > 0` (a valid NEP-141 behavior, e.g., the receiver only accepts part of the transfer).
5. The NEP-141 contract's `ft_resolve_transfer` returns `unused_amount` tokens back to Aurora's NEP-141 balance.
6. Aurora's callback `exit_to_near_precompile_callback` is invoked (if `error_refund` feature is enabled), but it only checks whether the promise succeeded or failed entirely — it does not inspect the partial refund amount.

The ERC-20 burn in step 1 is irreversible (it is part of the synchronous EVM execution). The `unused_amount` NEP-141 tokens accumulate on Aurora's balance with no mechanism to re-mint the corresponding ERC-20 tokens.

**Root cause in `exit_erc20_token_to_near` (Omni branch):** [1](#0-0) 

The `transfer_near_args` is `None` and the `refund` field (even when `error_refund` is enabled) encodes only the full-failure refund amount — not a partial-refund handler.

**Promise construction — no partial-refund callback:** [2](#0-1) 

**Callback only handles full failure, not partial refund:** [3](#0-2) 

When `ft_transfer_call` succeeds (even with partial refund), the callback enters the `PromiseResult::Successful` branch and does nothing beyond optionally transferring NEAR for the wNEAR unwrap case. The partial refund is silently absorbed into Aurora's NEP-141 balance.

---

### Impact Explanation

**Critical — Permanent freezing of funds.**

ERC-20 tokens are burned from the user's EVM balance during the synchronous EVM execution. If the NEP-141 receiver returns any portion of the transferred tokens, those NEP-141 tokens accumulate on Aurora's account with no corresponding ERC-20 re-mint. The user permanently loses the burned ERC-20 tokens, and the NEP-141 tokens are irrecoverably frozen on Aurora's balance.

---

### Likelihood Explanation

**Medium.** The Omni exit path (`ft_transfer_call`) is a production feature for cross-chain token routing. Any NEP-141 receiver that legitimately returns a non-zero `unused_amount` from `ft_on_transfer` (e.g., a bridge contract that rejects unknown messages, a contract with deposit limits, or a contract that only accepts specific token amounts) will trigger this freeze. No attacker capability is required beyond calling `exitToNear` with an Omni message targeting such a receiver.

---

### Recommendation

After `ft_transfer_call` succeeds, Aurora's callback must read the actual amount transferred (i.e., `total_amount - unused_amount`) from the `ft_resolve_transfer` result and re-mint the difference as ERC-20 tokens to the original sender. Concretely:

- Extend `ExitToNearPrecompileCallbackArgs` to carry the original sender's ERC-20 address and EVM address for the Omni path.
- In `exit_to_near_precompile_callback`, when the promise result is `Successful`, parse the returned value from `ft_resolve_transfer` (which is the amount actually transferred). If `refunded = original_amount - transferred > 0`, call `refund_on_error` to re-mint `refunded` ERC-20 tokens to the sender. [4](#0-3) [5](#0-4) 

---

### Proof of Concept

1. Deploy a NEP-141 token and bridge it to an ERC-20 on Aurora.
2. Deploy a NEAR receiver contract whose `ft_on_transfer` always returns the full `amount` (i.e., refuses all tokens).
3. Call `exitToNear` on the ERC-20 with an Omni message targeting this receiver.
4. Observe: ERC-20 tokens are burned from the user's EVM balance; the NEP-141 tokens are returned to Aurora's balance; the user has lost their tokens permanently.
5. Repeat with a receiver that returns only half the amount to demonstrate partial freeze.

The same outcome occurs with any legitimate receiver that partially rejects the transfer (e.g., a bridge with a deposit cap). [6](#0-5) [7](#0-6)

### Citations

**File:** engine-precompiles/src/native.rs (L444-483)
```rust
                ExitToNearParams::Erc20TokenParams(ref exit_params) => {
                    exit_erc20_token_to_near(context, exit_params, &self.io)?
                }
            };

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

**File:** engine-precompiles/src/native.rs (L610-647)
```rust
        // In this flow, we're just forwarding the `msg` to the `ft_transfer_call` transaction.
        Some(Message::Omni(msg)) => (
            nep141_account_id,
            ft_transfer_call_args(&exit_params.receiver_account_id, exit_params.amount, msg)?,
            "ft_transfer_call",
            None,
            events::ExitToNear::Omni(ExitToNearOmni {
                sender: Address::new(erc20_address),
                erc20_address: Address::new(erc20_address),
                dest: exit_params.receiver_account_id.to_string(),
                amount: exit_params.amount,
                msg: msg.to_string(),
            }),
        ),
        // The legacy flow. Just withdraw the tokens to the NEAR account id.
        // P.S. We use underscore here instead of `None` to handle the case when a user
        // could add the `unwrap` suffix for non wNEAR ERC-20 token by mistake.
        _ => {
            // There is no way to inject json, given the encoding of both arguments
            // as decimal and valid account id respectively.
            (
                nep141_account_id,
                format!(
                    r#"{{"receiver_id":"{}","amount":"{}"}}"#,
                    exit_params.receiver_account_id,
                    exit_params.amount.as_u128()
                ),
                "ft_transfer",
                None,
                events::ExitToNear::Legacy(ExitToNearLegacy {
                    sender: Address::new(erc20_address),
                    erc20_address: Address::new(erc20_address),
                    dest: exit_params.receiver_account_id.to_string(),
                    amount: exit_params.amount,
                }),
            )
        }
    };
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

**File:** engine/src/engine.rs (L1176-1204)
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
```
