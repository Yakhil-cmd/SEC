### Title
User-Controlled Omni Message in `exitToNear` Precompile Causes Permanent ERC-20 Token Loss When Destination Rejects `ft_transfer_call` — (File: engine-precompiles/src/native.rs)

---

### Summary
When a user calls the `exitToNear` precompile with an Omni-format message (`receiver_account_id:arbitrary_msg`), ERC-20 tokens are burned on Aurora **before** the outgoing `ft_transfer_call` promise is dispatched to the NEP-141 contract. If the destination's `ft_on_transfer` rejects the call, the NEP-141 standard refunds the tokens to the Aurora engine via `ft_resolve_transfer`, but the Aurora engine has no handler to re-mint the ERC-20 tokens. When the `error_refund` compile-time feature is absent, no callback is attached to the promise at all, making the ERC-20 token loss permanent.

---

### Finding Description

The `exitToNear` precompile (`engine-precompiles/src/native.rs`) parses the user-supplied input via `parse_recipient`. Any input of the form `receiver_account_id:some_msg` (where `some_msg` is not the literal `"unwrap"`) is classified as `Message::Omni(msg)`. [1](#0-0) 

In `exit_erc20_token_to_near`, the Omni branch:
1. Burns the ERC-20 tokens (irreversible).
2. Constructs a `ft_transfer_call` promise forwarding the user-supplied `msg` verbatim.
3. Sets `transfer_near_args = None`. [2](#0-1) 

The callback setup then evaluates:

```rust
let callback_args = ExitToNearPrecompileCallbackArgs {
    #[cfg(feature = "error_refund")]
    refund: refund_call_args(&exit_to_near_params, &exit_event),
    #[cfg(not(feature = "error_refund"))]
    refund: None,
    transfer_near: transfer_near_args, // always None for Omni
};

let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
    PromiseArgs::Create(transfer_promise) // ← NO callback attached
} else {
    PromiseArgs::Callback(PromiseWithCallbackArgs { ... })
};
``` [3](#0-2) 

When `error_refund` is **not** compiled in, `callback_args` equals `default()` (both fields `None`), so the promise is dispatched **without** a callback. The NEP-141 standard's `ft_resolve_transfer` will refund the tokens to the Aurora engine if the destination rejects the call, but the Aurora engine has no `exit_to_near_precompile_callback` attached to re-mint the ERC-20 tokens.

The callback that *would* handle this is:

```rust
} else if let Some(args) = args.refund {
    // Exit call failed; need to refund tokens
    let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;
    ...
}
``` [4](#0-3) 

This branch is never reached for Omni exits when `error_refund` is disabled, because no callback is scheduled.

The user-controlled entry path is the `msg` field forwarded verbatim into `ft_transfer_call_args`: [5](#0-4) 

---

### Impact Explanation

Permanent loss of bridged ERC-20 tokens. The NEP-141 tokens accumulate in the Aurora engine's NEP-141 balance with no mechanism to convert them back to ERC-20 for the original user. This is a **Critical** permanent fund freeze / direct loss of user funds in motion.

---

### Likelihood Explanation

Any EVM user who calls `exitToNear` with an Omni message (`:msg` suffix) targeting a NEAR contract whose `ft_on_transfer` rejects the call — due to wrong message format, contract-specific validation, or a contract that does not implement `ft_on_transfer` — will trigger this path. The Omni message feature is user-facing. The `error_refund` feature flag is a compile-time gate; if the production WASM is built without it (which is the non-default path shown in the `#[cfg(not(feature = "error_refund"))]` branch), every Omni-path exit is vulnerable.

---

### Recommendation

1. Unconditionally attach `exit_to_near_precompile_callback` whenever `ft_transfer_call` is used (i.e., for all Omni-path exits), regardless of the `error_refund` feature flag.
2. Inside the callback, re-mint the ERC-20 tokens if the promise result indicates failure.
3. Alternatively, mandate that the `error_refund` feature is always enabled for any production build that exposes the Omni message path.

---

### Proof of Concept

1. User holds 1 000 bridged USDC (ERC-20) on Aurora.
2. User calls the USDC ERC-20 `withdraw` function encoding the destination as `receiver.near:{"bad":"json"}` (Omni format).
3. The ERC-20 contract burns 1 000 USDC and calls the `exitToNear` precompile.
4. `exitToNear` dispatches `ft_transfer_call` to the USDC NEP-141 contract with `msg = '{"bad":"json"}'` and **no callback**.
5. The USDC NEP-141 contract calls `ft_on_transfer` on `receiver.near`; `receiver.near` rejects the call and returns the full amount.
6. The USDC NEP-141 contract refunds 1 000 USDC to the Aurora engine via `ft_resolve_transfer`.
7. No callback exists on the Aurora side to re-mint ERC-20 tokens.
8. The user's 1 000 USDC ERC-20 tokens are permanently destroyed; the NEP-141 tokens sit in the Aurora engine's balance, inaccessible to the user.

### Citations

**File:** engine-precompiles/src/native.rs (L359-378)
```rust
fn parse_recipient(recipient: &[u8]) -> Result<Recipient<'_>, ExitError> {
    let recipient = str::from_utf8(recipient)
        .map_err(|_| ExitError::Other(Cow::from("ERR_INVALID_RECEIVER_ACCOUNT_ID")))?;
    let (receiver_account_id, message) = recipient.split_once(':').map_or_else(
        || (recipient, None),
        |(recipient, msg)| {
            if msg == UNWRAP_WNEAR_MSG {
                (recipient, Some(Message::UnwrapWnear))
            } else {
                (recipient, Some(Message::Omni(msg)))
            }
        },
    );

    Ok(Recipient {
        receiver_account_id: receiver_account_id
            .parse()
            .map_err(|_| ExitError::Other(Cow::from("ERR_INVALID_RECEIVER_ACCOUNT_ID")))?,
        message,
    })
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

**File:** engine-precompiles/src/native.rs (L800-815)
```rust
fn ft_transfer_call_args(
    receiver_id: &AccountId,
    amount: U256,
    msg: &str,
) -> Result<String, ExitError> {
    if amount > U256::from(u128::MAX) {
        return Err(ExitError::Other(Cow::from("ERR_INVALID_AMOUNT")));
    }

    serde_json::to_string(&FtTransferCallArgs {
        receiver_id,
        amount: amount.to_string(),
        msg,
    })
    .map_err(|_| ExitError::Other(Cow::from("ERR_SERIALIZE_JSON")))
}
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
