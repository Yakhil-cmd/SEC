### Title
`ExitToNear` Omni `ft_transfer_call` Partial-Return Tokens Permanently Frozen in Aurora's NEP-141 Balance — (`engine-precompiles/src/native.rs`)

---

### Summary

When the `ExitToNear` precompile is invoked with an Omni message (`:msg` suffix), it burns the user's ERC-20 tokens on the EVM side and calls `ft_transfer_call` on the corresponding NEP-141 contract. Per the NEP-141 standard, the receiver's `ft_on_transfer` may return a non-zero amount of unused tokens, which the NEP-141 contract's `ft_resolve_transfer` refunds back to the sender — Aurora's engine account. Because the `ExitToNear` precompile attaches **no callback** for the Omni path (when the `error_refund` feature is disabled) and the existing callback only handles full promise failure (not partial returns), the refunded NEP-141 tokens accumulate in Aurora's account with no corresponding ERC-20 tokens and no mechanism to re-mint them. The tokens are permanently frozen.

---

### Finding Description

**Root cause — no callback attached for the Omni `ft_transfer_call` path:**

In `exit_erc20_token_to_near`, the Omni branch sets `transfer_near_args = None`: [1](#0-0) 

Similarly for the base-token Omni branch: [2](#0-1) 

Back in `ExitToNear::run`, the callback decision is:

```rust
let callback_args = ExitToNearPrecompileCallbackArgs {
    #[cfg(not(feature = "error_refund"))]
    refund: None,
    transfer_near: transfer_near_args,   // None for Omni path
};

let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
    PromiseArgs::Create(transfer_promise)   // ← no callback attached
} else {
    PromiseArgs::Callback(PromiseWithCallbackArgs { ... })
};
``` [3](#0-2) 

When the `error_refund` feature is **disabled** (the default non-refund build), both `refund` and `transfer_near` are `None`, so `callback_args` equals `Default::default()`, and the engine schedules a bare `PromiseArgs::Create` — no callback whatsoever.

**Even with `error_refund` enabled, partial returns are not handled:**

The `exit_to_near_precompile_callback` only acts on a **full failure** of the promise:

```rust
if let Some(PromiseResult::Successful(_)) = handler.promise_result(0) {
    // Omni path: transfer_near is None → nothing happens
    None
} else if let Some(args) = args.refund {
    // Only reached on full failure
    engine::refund_on_error(...)
}
``` [4](#0-3) 

A **partial return** — where `ft_on_transfer` returns `k < amount` tokens — causes the NEP-141 contract's internal `ft_resolve_transfer` to credit those `k` tokens back to Aurora's engine account. Aurora has no callback to observe this credit and re-mint the corresponding ERC-20 tokens. The ERC-20 tokens were already burned; the NEP-141 tokens are now orphaned in Aurora's balance.

The `ExitToNearPrecompileCallbackArgs` struct confirms the two-field design that was never extended to cover partial-return accounting: [5](#0-4) 

---

### Impact Explanation

**Critical — Permanent freezing of funds.**

Every time a NEAR DeFi contract's `ft_on_transfer` returns a non-zero unused amount, those NEP-141 tokens are credited to Aurora's engine account. There is no contract method to re-mint ERC-20 tokens from an "excess" NEP-141 balance, and the only exit path (`ExitToNear`) requires burning ERC-20 tokens that no longer exist. The tokens are irrecoverably frozen.

---

### Likelihood Explanation

**Medium.** The Omni path is the standard mechanism for Aurora users to interact with NEAR DeFi protocols (e.g., AMMs, lending markets) that implement `ft_on_transfer`. Any protocol that accepts only a portion of the transferred amount — a common pattern — will trigger the partial-return path. No special attacker capability is required; any ordinary EVM user calling `ExitToNear` with a `:msg` suffix and a receiver that returns tokens is sufficient.

---

### Recommendation

1. For the Omni `ft_transfer_call` path, always attach a callback (`exit_to_near_precompile_callback`) that reads the promise result of `ft_transfer_call`.
2. In the callback, decode the return value of `ft_transfer_call` (which is the amount refunded by `ft_resolve_transfer`) and re-mint the corresponding ERC-20 tokens to the original sender's EVM address.
3. Alternatively, record the original sender's address in the callback args (analogous to `RefundCallArgs`) so the re-mint target is unambiguous.

---

### Proof of Concept

1. User holds 1000 units of ERC-20 token `T` on Aurora (backed 1:1 by NEP-141 `T` held by the engine account).
2. User calls `ExitToNear` precompile with flag `0x1`, amount `1000`, recipient `amm.near`, and Omni message `{"action":"swap","min_out":"900"}`.
3. ERC-20 `T` tokens (1000) are burned on the EVM side.
4. `ft_transfer_call("amm.near", 1000, '{"action":"swap","min_out":"900"}')` is scheduled on the NEP-141 contract.
5. `amm.near`'s `ft_on_transfer` executes the swap and returns `100` (unused tokens).
6. NEP-141's `ft_resolve_transfer` credits 100 tokens back to Aurora's engine account.
7. Aurora's NEP-141 balance for `T` is now `100` above the total ERC-20 supply.
8. No callback fires; no ERC-20 tokens are re-minted.
9. The 100 NEP-141 tokens are permanently frozen — the user lost them with no recourse. [6](#0-5) [1](#0-0) [4](#0-3)

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

**File:** engine-precompiles/src/native.rs (L519-534)
```rust
        Some(Message::Omni(msg)) => Ok((
            eth_connector_account_id,
            ft_transfer_call_args(
                &exit_params.receiver_account_id,
                context.apparent_value,
                msg,
            )?,
            events::ExitToNear::Omni(ExitToNearOmni {
                sender: Address::new(context.caller),
                erc20_address: events::ETH_ADDRESS,
                dest: exit_params.receiver_account_id.to_string(),
                amount: context.apparent_value,
                msg: msg.to_string(),
            }),
            "ft_transfer_call".to_string(),
            None,
```

**File:** engine-precompiles/src/native.rs (L610-623)
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

**File:** engine-types/src/parameters/connector.rs (L130-134)
```rust
#[derive(Debug, Clone, BorshSerialize, BorshDeserialize, PartialEq, Eq, Default)]
pub struct ExitToNearPrecompileCallbackArgs {
    pub refund: Option<RefundCallArgs>,
    pub transfer_near: Option<TransferNearArgs>,
}
```
