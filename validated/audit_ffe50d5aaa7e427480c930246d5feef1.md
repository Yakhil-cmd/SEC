### Title
Burned ERC-20 / ETH Funds Permanently Lost When `ft_transfer_call` (Omni) Fails Without `error_refund` Feature - (File: `engine-precompiles/src/native.rs`)

### Summary

When the `ExitToNear` precompile is invoked via the Omni path (i.e., the user appends a `:msg` suffix to the recipient, triggering `ft_transfer_call` instead of `ft_transfer`), and the `error_refund` Cargo feature is not enabled, no callback promise is attached to the outgoing `ft_transfer_call`. If the downstream NEP-141 receiver rejects the tokens (partially or fully), the NEP-141 tokens are returned to Aurora's account by the NEP-141 standard's `ft_resolve_transfer` mechanism, but the ERC-20 tokens that were already burned on the EVM side are never re-minted. The user permanently loses their funds.

### Finding Description

The `ExitToNear::run()` function in `engine-precompiles/src/native.rs` constructs a `callback_args` struct and decides whether to attach a callback promise based on whether `callback_args` equals its default value: [1](#0-0) 

The `refund` field is only populated when the `error_refund` feature is compiled in: [2](#0-1) 

The `transfer_near` field is only `Some(...)` for the wNEAR unwrap path (`near_withdraw`). For the Omni path (`ft_transfer_call`), both `exit_base_token_to_near` and `exit_erc20_token_to_near` return `transfer_near_args = None`: [3](#0-2) [4](#0-3) 

When `error_refund` is not enabled and the Omni path is used, `callback_args` equals `ExitToNearPrecompileCallbackArgs::default()` (both fields `None`), so the promise degrades to a bare `PromiseArgs::Create` with no error-handling callback: [5](#0-4) 

The `exit_to_near_precompile_callback` in `engine/src/contract_methods/connector.rs` is the only place where a refund (re-mint of burned ERC-20 tokens, or ETH restoration) can occur, but it is never scheduled for this path: [6](#0-5) 

The existing test suite explicitly acknowledges this gap: [7](#0-6) 

**Step-by-step attack path:**

1. User holds ERC-20 tokens on Aurora (backed by NEP-141 tokens held by Aurora's account).
2. User calls the ERC-20's `withdraw` function with an Omni message (e.g., `receiver.near:{...}`). The ERC-20 contract burns the tokens and calls the `ExitToNear` precompile.
3. The precompile schedules a bare `ft_transfer_call` on the NEP-141 contract with no callback.
4. The NEP-141 receiver's `ft_on_transfer` rejects the tokens (returns the full amount as unused), causing `ft_resolve_transfer` to refund the NEP-141 tokens back to Aurora's account.
5. Aurora's NEP-141 balance is restored, but the ERC-20 tokens were already burned and no re-mint is triggered. The user's funds are permanently lost.

The same applies to the ETH (base token) Omni path: ETH is deducted from the user's EVM balance and sent to the `ExitToNear` precompile address, but if `ft_transfer_call` fails, no ETH is restored.

### Impact Explanation

**Critical — Permanent freezing of user funds.** The user's ERC-20 tokens (or ETH) are irreversibly burned/deducted on the EVM side. The corresponding NEP-141 tokens are returned to Aurora's contract account by the NEP-141 standard, but there is no mechanism to forward them back to the user or re-mint the ERC-20 tokens. The funds are permanently inaccessible to the user.

### Likelihood Explanation

**High.** Any user who uses the Omni exit path (a documented and supported feature) and whose target receiver rejects the `ft_transfer_call` — due to wrong message format, unregistered account, insufficient storage deposit, or any other reason — will lose their funds. This requires no special privileges and is triggered by normal user interaction with the intended production interface.

### Recommendation

When the Omni path (`ft_transfer_call`) is used, always attach a callback promise regardless of whether `error_refund` is enabled. The callback should check the promise result and, on failure, re-mint the burned ERC-20 tokens (or restore the ETH balance) to the user's address. Specifically:

- In `exit_base_token_to_near` and `exit_erc20_token_to_near`, populate a refund address (even without the `error_refund` feature) so that `callback_args` is never default when `ft_transfer_call` is used.
- Alternatively, always attach the `exit_to_near_precompile_callback` when the method is `ft_transfer_call`, unconditionally.

### Proof of Concept

1. Deploy an ERC-20 token on Aurora backed by a NEP-141 token.
2. Call the ERC-20's `withdraw` with input flag `0x01` (ERC-20 exit), amount `N`, and recipient `receiver.near:{some_msg}` (Omni path).
3. The ERC-20 burns `N` tokens; the precompile schedules `ft_transfer_call` on the NEP-141 with no callback.
4. Deploy a NEAR contract at `receiver.near` that implements `ft_on_transfer` and returns the full amount (rejects all tokens).
5. Observe: the NEP-141 tokens are returned to Aurora's account via `ft_resolve_transfer`, but the user's ERC-20 balance remains at zero. The `N` tokens are permanently lost.

The test at `engine-tests/src/tests/erc20_connector.rs:656-660` already documents that without `error_refund`, the user's balance is `FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT` (i.e., the exit amount is lost), confirming the impact. [5](#0-4) [2](#0-1) [6](#0-5)

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

**File:** engine-tests/src/tests/erc20_connector.rs (L656-660)
```rust
        #[cfg(feature = "error_refund")]
        let balance = FT_TRANSFER_AMOUNT.into();
        // If the refund feature is not enabled then there is no refund in the EVM
        #[cfg(not(feature = "error_refund"))]
        let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();
```
