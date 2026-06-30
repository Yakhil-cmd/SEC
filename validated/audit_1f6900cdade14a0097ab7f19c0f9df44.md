### Title
Burned ERC-20 Tokens Permanently Lost When `ft_transfer_call` Receiver Returns Unused Tokens in Omni Exit Flow — (`engine-precompiles/src/native.rs`)

---

### Summary

When the `ExitToNear` precompile is invoked with an Omni message (triggering `ft_transfer_call` to a NEP-141 contract), the ERC-20 tokens are burned on the EVM side before the cross-chain call is made. If the NEP-141 receiver's `ft_on_transfer` returns a non-zero unused amount, the NEP-141 standard's `ft_resolve_transfer` refunds those tokens back to Aurora's account. However, no callback exists to re-mint the corresponding ERC-20 tokens. The refunded NEP-141 tokens are permanently stranded in Aurora's account with no recovery path, while the user's ERC-20 tokens are irreversibly burned.

---

### Finding Description

The `ExitToNear` precompile's `run()` function constructs a `callback_args` struct and conditionally attaches a callback promise only when `callback_args != default()`. [1](#0-0) 

For the Omni ERC-20 exit path (`Message::Omni`), `exit_erc20_token_to_near` returns `transfer_near_args = None`: [2](#0-1) 

Combined with `refund: None` (when the `error_refund` feature is not compiled in), `callback_args` equals `ExitToNearPrecompileCallbackArgs::default()`, so the branch at line 470 selects `PromiseArgs::Create(transfer_promise)` — a bare `ft_transfer_call` with **no callback**: [3](#0-2) 

Even when `error_refund` is enabled and a callback is attached, `exit_to_near_precompile_callback` only handles two cases: (1) success + wNEAR unwrap (`transfer_near`), and (2) complete failure + refund. It has **no branch** for the case where `ft_transfer_call` succeeds but the receiver's `ft_on_transfer` returns a non-zero unused amount: [4](#0-3) 

The NEP-141 standard guarantees that `ft_resolve_transfer` will refund unused tokens back to the sender (Aurora). Those refunded NEP-141 tokens accumulate in Aurora's account with no corresponding ERC-20 tokens minted, and no mechanism exists to claim them.

---

### Impact Explanation

**Permanent freezing of funds (Critical).**

The user's ERC-20 tokens are burned atomically at the EVM level before the cross-chain promise executes. If the NEP-141 receiver returns any unused amount `X` (where `0 < X ≤ amount`):

- The user loses `amount` ERC-20 tokens permanently (burned, not re-minted).
- `X` NEP-141 tokens are refunded to Aurora's NEP-141 balance by `ft_resolve_transfer`.
- No ERC-20 tokens are minted for the refunded `X` NEP-141 tokens.
- The `X` NEP-141 tokens are stranded in Aurora's account with no recovery path for the original user.

---

### Likelihood Explanation

**Medium.** The Omni exit path is a documented, user-facing feature reachable by any EVM user who calls an ERC-20 contract's `withdrawToNear` with a colon-delimited message (e.g., `receiver.near:some_msg`). Any NEP-141 receiver that partially consumes the transferred tokens — by design or due to slippage, limits, or a malicious implementation — triggers the loss. No privileged access is required.

---

### Recommendation

After a successful `ft_transfer_call` in the Omni flow, attach a callback that reads the `ft_resolve_transfer` result (the amount actually consumed) and re-mints ERC-20 tokens for the unused portion returned to Aurora. Specifically:

1. Always attach `exit_to_near_precompile_callback` when `ft_transfer_call` is used (Omni path), regardless of the `error_refund` feature flag.
2. In `exit_to_near_precompile_callback`, on a successful promise result, read the returned value from `ft_resolve_transfer` (the refunded amount) and call `refund_on_error` or an equivalent mint path to restore ERC-20 tokens for the refunded NEP-141 amount.

---

### Proof of Concept

1. User holds 1000 units of ERC-20 token `T` on Aurora (backed by NEP-141 `t.near`).
2. User calls `T.withdrawToNear("receiver.near:some_msg", 1000)`.
3. ERC-20 contract burns 1000 `T` tokens and calls the `ExitToNear` precompile with `Message::Omni("some_msg")`.
4. Precompile constructs `callback_args { refund: None, transfer_near: None }` → equals `default()` → no callback is attached.
5. `ft_transfer_call("receiver.near", 1000, "some_msg")` is dispatched to `t.near`.
6. `receiver.near`'s `ft_on_transfer` processes 600 tokens and returns `"400"` (unused).
7. `t.near`'s `ft_resolve_transfer` refunds 400 tokens back to Aurora's `t.near` balance.
8. Result: User has 0 `T` ERC-20 tokens. `receiver.near` has 600 `t.near` tokens. Aurora has 400 `t.near` tokens with no corresponding ERC-20 tokens and no recovery mechanism. [5](#0-4) [6](#0-5) [7](#0-6)

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

**File:** engine-precompiles/src/native.rs (L611-623)
```rust
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
```

**File:** engine/src/contract_methods/connector.rs (L214-242)
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
```
