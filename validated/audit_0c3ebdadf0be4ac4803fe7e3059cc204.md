### Title
Unused Tokens From Partial `ft_transfer_call` Fill Not Refunded to User in `ExitToNear` Omni Path — (`engine-precompiles/src/native.rs`, `engine/src/contract_methods/connector.rs`)

---

### Summary

The `ExitToNear` precompile's Omni message path uses NEP-141 `ft_transfer_call` to bridge ERC-20 tokens to NEAR. When the NEAR-side receiver contract partially consumes the transferred tokens and returns an unused remainder, the NEP-141 standard refunds that remainder back to Aurora Engine's own NEP-141 balance. However, `exit_to_near_precompile_callback` silently discards the promise return value (which encodes the refunded amount) and never re-credits the user's EVM balance. The unused tokens are permanently stranded in Aurora Engine's NEP-141 balance.

---

### Finding Description

**Step 1 — Token burn and `ft_transfer_call` scheduling**

When a user exits ERC-20 tokens to NEAR with an Omni message, `exit_erc20_token_to_near` selects `method = "ft_transfer_call"` and `transfer_near_args = None`: [1](#0-0) 

The precompile then schedules a `PromiseWithCallbackArgs` whose base is the `ft_transfer_call` and whose callback is `exit_to_near_precompile_callback`: [2](#0-1) 

At this point the ERC-20 tokens have already been burned from the user's EVM balance.

**Step 2 — Callback ignores the refunded amount**

In the NEP-141 standard, `ft_transfer_call` returns the number of tokens the receiver did **not** consume. The token contract refunds that amount back to the caller (Aurora Engine). The promise result therefore encodes the refunded quantity.

`exit_to_near_precompile_callback` checks only whether the promise succeeded or failed; it discards the return value with `_`: [3](#0-2) 

When the promise succeeds and `transfer_near` is `None` (the Omni case), the callback returns `None` without inspecting the refunded amount. The `error_refund` branch is only reached on complete promise failure, not on partial fills: [4](#0-3) 

**Step 3 — No minimum-output protection**

The `ExitToNear` precompile input format accepts no `min_amount_out` parameter. The user has no way to assert a lower bound on how many tokens the NEAR-side receiver must actually consume: [5](#0-4) 

---

### Impact Explanation

A user burns ERC-20 tokens from their EVM balance and calls `ExitToNear` with an Omni message. If the NEAR-side receiver's `ft_on_transfer` returns a non-zero unused amount, the NEP-141 contract refunds those tokens to Aurora Engine's own NEP-141 balance. Because the callback ignores the refunded quantity and performs no re-mint, the user's EVM balance is permanently short by that amount. The tokens are irrecoverably stranded in Aurora Engine's NEP-141 account.

**Impact class:** Permanent freezing / direct theft of user funds (Critical).

---

### Likelihood Explanation

Any EVM user can trigger this path by calling an ERC-20 exit function that routes through `ExitToNear` with an Omni message. The NEAR-side receiver can be any contract that legitimately returns a partial unused amount from `ft_on_transfer` (e.g., a DEX that fills only part of an order, a lending protocol that caps deposits, or any contract that enforces its own limits). This is an intended and common pattern in the NEP-141 ecosystem, making the scenario realistic without requiring any attacker-controlled contract.

---

### Recommendation

1. **Read and act on the `ft_transfer_call` return value.** In `exit_to_near_precompile_callback`, decode `PromiseResult::Successful(bytes)` to obtain the refunded token amount. If it is non-zero, re-mint that amount of ERC-20 tokens to the original sender's EVM address (analogous to what `refund_on_error` does for complete failures).

2. **Add a `min_amount_out` parameter to the `ExitToNear` Omni path.** Allow callers to specify the minimum number of tokens the receiver must consume. If the refunded amount would exceed `total - min_amount_out`, revert or trigger the refund path immediately.

---

### Proof of Concept

1. Alice holds 1000 `TOKEN` ERC-20 on Aurora.
2. Alice calls `TOKEN.withdrawTo(omni_receiver, 1000, "some_msg")`, which triggers `ExitToNear` with `Message::Omni("some_msg")`.
3. The precompile burns 1000 `TOKEN` from Alice's EVM balance and schedules `ft_transfer_call(omni_receiver, 1000, "some_msg")` on the NEP-141 contract, with `exit_to_near_precompile_callback` as the callback.
4. `omni_receiver.ft_on_transfer` processes only 600 tokens and returns `"400"` (unused).
5. The NEP-141 contract refunds 400 tokens to Aurora Engine's NEP-141 balance.
6. `exit_to_near_precompile_callback` fires with `PromiseResult::Successful(b"\"400\"")`. The callback matches the success branch, finds `transfer_near = None`, and returns `None` — the `"400"` is never read.
7. Alice received only 600 tokens on NEAR. Her EVM balance was debited 1000. The 400-token difference is permanently locked in Aurora Engine's NEP-141 balance with no recovery path. [6](#0-5) [1](#0-0)

### Citations

**File:** engine-precompiles/src/native.rs (L394-419)
```rust
        // ETH (base) transfer input format: (85 bytes)
        //  - flag (1 byte)
        //  - refund_address (20 bytes), present if the feature "error_refund" is enabled
        //  - recipient_account_id (max MAX_INPUT_SIZE - 20 - 1 bytes)
        // ERC-20 transfer input format: (124 bytes)
        //  - flag (1 byte)
        //  - refund_address (20 bytes), present if the feature "error_refund" is enabled.
        //  - amount (32 bytes)
        //  - recipient_account_id (max MAX_INPUT_SIZE - 1 - (20) - 32 bytes)
        //  - `:unwrap` suffix in a case of wNEAR (7 bytes)
        let required_gas = Self::required_gas(input)?;

        if let Some(target_gas) = target_gas
            && required_gas > target_gas
        {
            return Err(ExitError::OutOfGas);
        }

        // It's not allowed to call exit precompiles in static mode
        if is_static {
            return Err(ExitError::Other(Cow::from("ERR_INVALID_IN_STATIC")));
        } else if context.address != exit_to_near::ADDRESS.raw() {
            return Err(ExitError::Other(Cow::from("ERR_INVALID_IN_DELEGATE")));
        }

        let exit_to_near_params = ExitToNearParams::try_from(input)?;
```

**File:** engine-precompiles/src/native.rs (L470-483)
```rust
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

**File:** engine/src/contract_methods/connector.rs (L213-230)
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

**File:** engine/src/contract_methods/connector.rs (L231-242)
```rust
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
