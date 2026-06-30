### Title
Permanent Freeze of NEP-141 Tokens When `ft_transfer_call` Partially Succeeds in `ExitToNear` Omni Path — (File: `engine-precompiles/src/native.rs`)

---

### Summary

In the `ExitToNear` precompile's Omni message path, ERC-20 tokens are burned from the user's EVM balance upfront for the full requested amount. If the downstream `ft_transfer_call` receiver returns any unused tokens (a valid NEP-141 behavior), those NEP-141 tokens are silently credited back to Aurora's NEP-141 balance with no corresponding ERC-20 re-minting. The `exit_to_near_precompile_callback` does not inspect the promise return value and does not handle this partial-return case, resulting in permanently locked NEP-141 tokens.

---

### Finding Description

**Step 1 — ERC-20 burn and promise construction**

When a user calls `withdrawToNear(bytes dest, uint256 amount)` on an ERC-20 contract with an Omni-style destination (e.g., `"receiver.near:{...json...}"`), the ERC-20 contract burns `amount` tokens from the caller's balance and calls the `ExitToNear` precompile. Inside `ExitToNear::run()`, the Omni branch of `exit_erc20_token_to_near` is taken: [1](#0-0) 

This produces `method = "ft_transfer_call"` and `transfer_near_args = None`. The promise is then constructed: [2](#0-1) 

**Step 2 — Callback registration gap**

With the `error_refund` feature enabled, `callback_args.refund` is `Some(...)` and `callback_args.transfer_near` is `None`. Because `callback_args != default()`, a callback to `exit_to_near_precompile_callback` is registered. Without `error_refund`, `callback_args == default()` and **no callback is registered at all** — making the problem even worse. [3](#0-2) 

**Step 3 — Callback does not handle partial return**

`exit_to_near_precompile_callback` handles exactly two outcomes:

- Promise **failed** → call `refund_on_error` to re-mint ERC-20 tokens (only with `error_refund`).
- Promise **succeeded** + `transfer_near` is `Some` → transfer unwrapped NEAR (wNEAR path only).

For the Omni path, `transfer_near` is always `None`. So when the promise **succeeds**, the callback does nothing: [4](#0-3) 

**Step 4 — NEP-141 partial return mechanics**

In the NEP-141 standard, `ft_transfer_call` calls `receiver.ft_on_transfer(sender, amount, msg)`. The receiver returns `unused_amount`. The NEP-141 contract's `ft_resolve_transfer` then credits `unused_amount` back to the sender (Aurora's NEP-141 account). The promise result seen by the callback is `Successful(bytes_of_amount_used)` — the callback reads only whether it succeeded, not how much was actually consumed: [5](#0-4) 

The `unused_amount` NEP-141 tokens land in Aurora's NEP-141 balance. No ERC-20 tokens are minted for them. The ERC-20 tokens were already burned for the full `amount`. The `unused_amount` NEP-141 tokens are permanently locked.

This is confirmed by the existing test that explicitly acknowledges the no-refund behavior when `error_refund` is absent: [6](#0-5) 

---

### Impact Explanation

**Critical — Permanent freezing of funds.**

For every `ft_transfer_call` exit where the receiver returns `unused_amount > 0` tokens:

- The user's ERC-20 balance is reduced by the full `amount` (burned, irreversible in EVM state).
- Aurora's NEP-141 balance increases by `unused_amount` (returned by the NEP-141 contract).
- No ERC-20 tokens are minted for `unused_amount`.
- `unused_amount` NEP-141 tokens are permanently stranded in Aurora's NEP-141 balance with no ERC-20 counterpart and no recovery path.

The total NEP-141 supply held by Aurora exceeds the total ERC-20 supply, creating a permanent insolvency in the bridge accounting for those tokens.

---

### Likelihood Explanation

**Medium.** The Omni message path (`ft_transfer_call`) is the intended mechanism for cross-chain transfers via the Omni bridge. Any receiver contract that implements `ft_on_transfer` and returns a non-zero `unused_amount` triggers this path. Realistic triggers include:

- An Omni bridge receiver that partially processes a transfer (e.g., applies a fee, hits a cap, or encounters a partial failure) and returns the remainder.
- Any user-controlled or third-party contract registered as a receiver that legitimately returns unused tokens per the NEP-141 spec.

The condition is reachable by any unprivileged EVM user who holds ERC-20 tokens and calls `withdrawToNear` with an Omni-style destination.

---

### Recommendation

In `exit_to_near_precompile_callback`, when the promise result is `Successful`, parse the returned bytes to extract the amount actually transferred (the value returned by `ft_resolve_transfer`). Compute `unused_amount = original_amount - transferred_amount`. If `unused_amount > 0`, mint the corresponding ERC-20 tokens back to the original caller's address (analogous to what `refund_on_error` does for complete failures). [7](#0-6) 

---

### Proof of Concept

1. User holds 100 ERC-20 tokens (backed by 100 NEP-141 tokens in Aurora's NEP-141 balance).
2. User calls `withdrawToNear(bytes("receiver.near:{...omni_msg...}"), 100)` on the ERC-20 contract.
3. ERC-20 contract burns 100 tokens from user's EVM balance.
4. `ExitToNear` precompile schedules `nep141.ft_transfer_call(receiver.near, 100, "{...omni_msg...}")` with a callback to `exit_to_near_precompile_callback`.
5. `receiver.near.ft_on_transfer(aurora, 100, "{...omni_msg...}")` returns `30` (unused).
6. NEP-141 `ft_resolve_transfer` credits 30 tokens back to Aurora's NEP-141 balance. Promise result: `Successful("70")`.
7. `exit_to_near_precompile_callback` fires, sees `Successful`, `transfer_near = None` → does nothing.
8. **Final state**: User lost 100 ERC-20 tokens. Aurora's NEP-141 balance increased by 30 (net reduction of 70, not 100). 30 NEP-141 tokens are permanently locked in Aurora's NEP-141 balance with no ERC-20 representation and no recovery mechanism. [8](#0-7) [9](#0-8)

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

**File:** engine/src/contract_methods/connector.rs (L195-246)
```rust
#[named]
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

**File:** engine-tests/src/tests/erc20_connector.rs (L656-660)
```rust
        #[cfg(feature = "error_refund")]
        let balance = FT_TRANSFER_AMOUNT.into();
        // If the refund feature is not enabled then there is no refund in the EVM
        #[cfg(not(feature = "error_refund"))]
        let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();
```
