### Title
`exit_to_near` Omni Bridge Path Ignores Partial `ft_transfer_call` Return, Causing Permanent Loss of Bridged Funds - (File: `engine/src/contract_methods/connector.rs`, `engine-precompiles/src/native.rs`)

---

### Summary

When a user exits ERC-20 or ETH tokens to NEAR via the Omni bridge path (using `ft_transfer_call`), the `exit_to_near_precompile_callback` only checks whether the promise succeeded or failed. It never inspects the **actual amount transferred** returned by `ft_resolve_transfer`. If the Omni bridge receiver's `ft_on_transfer` returns a non-zero amount (a partial refund, which is valid NEP-141 behavior), the `ft_transfer_call` promise still resolves as `Successful`, but only part of the tokens reach the destination. The remainder is credited back to Aurora's NEP-141 balance. Because the ERC-20 tokens were already burned for the full amount before the promise was dispatched, the user permanently loses the difference with no recourse.

---

### Finding Description

**Bridge exit flow (Omni path)**

When a user calls the `exit_to_near` precompile with a message containing `:` followed by a non-`unwrap` suffix, the code routes through `Message::Omni(msg)`.

For ERC-20 tokens, `exit_erc20_token_to_near` selects `"ft_transfer_call"` as the method and sets `transfer_near_args = None`: [1](#0-0) 

For the base ETH token, `exit_base_token_to_near` does the same: [2](#0-1) 

The precompile then builds `callback_args`. With the `error_refund` feature enabled, `refund` is populated (so a callback is attached). Without it, `callback_args` equals `Default` and **no callback is attached at all** — meaning even a total failure produces no refund. [3](#0-2) 

**The callback ignores the transferred amount**

When a callback is attached (i.e., `error_refund` is enabled), `exit_to_near_precompile_callback` handles the promise result: [4](#0-3) 

The branch `if let Some(PromiseResult::Successful(_)) = handler.promise_result(0)` uses a wildcard `_` to discard the bytes returned by the promise. In NEAR's NEP-141 standard, `ft_transfer_call` resolves through `ft_resolve_transfer`, which returns a `U128` encoding the number of tokens **actually transferred** (i.e., `amount - refunded`). If the Omni bridge receiver's `ft_on_transfer` returns `R > 0`, then `ft_resolve_transfer` refunds `R` tokens back to Aurora's NEP-141 balance and returns `amount - R`. The promise is still `Successful`. Aurora's callback sees `Successful` and takes the no-op path, never re-minting the `R` tokens that were refunded.

**ERC-20 tokens are burned before the promise is dispatched**

The ERC-20 burn happens inside the EVM execution (the ERC-20 contract calls the precompile after burning). By the time the NEAR promise is scheduled, the full `amount` of ERC-20 tokens is already gone from the user's balance. There is no mechanism to reverse this burn if the bridge only partially accepts the tokens. [5](#0-4) 

**Stranded NEP-141 tokens**

The `R` refunded NEP-141 tokens land in Aurora's NEP-141 balance. Aurora's NEP-141 balance can only be reduced by burning ERC-20 tokens via the `exit_to_near` precompile. Since no ERC-20 tokens exist for the refunded amount, those NEP-141 tokens are permanently frozen inside Aurora's account with no recovery path for the user.

---

### Impact Explanation

**Critical — Permanent freezing of funds / direct theft of user funds.**

A user who exits tokens via the Omni bridge path loses the portion of tokens that the bridge receiver rejects. The ERC-20 tokens are irreversibly burned; the corresponding NEP-141 tokens are locked in Aurora's balance forever. The discrepancy between Aurora's ERC-20 supply and its NEP-141 backing grows with each such event, eventually causing insolvency of the bridge peg.

---

### Likelihood Explanation

**Medium.** The Omni bridge receiver (`ft_on_transfer`) returning a non-zero amount is a standard, documented NEP-141 behavior. It occurs in production when:
- The bridge fee encoded in `msg` is insufficient.
- The recipient address in `msg` is unsupported or invalid.
- The bridge has a minimum transfer threshold.
- The bridge is rate-limited or paused for a specific token.

Any user interacting with the Omni bridge exit path is exposed. No special privilege is required — any EVM account holding ERC-20 tokens can trigger this path.

---

### Recommendation

In `exit_to_near_precompile_callback`, when the promise result is `Successful`, decode the returned `U128` bytes and compare the transferred amount against the original amount stored in `callback_args`. If `transferred < original`, re-mint `original - transferred` ERC-20 tokens (or credit ETH) to the user's refund address using the same `refund_on_error` / `setup_refund_on_error_input` machinery already present for the full-failure case. [6](#0-5) 

---

### Proof of Concept

1. User holds 100 units of ERC-20 token `T` on Aurora (backed by 100 NEP-141 tokens in Aurora's balance).
2. User calls the `exit_to_near` precompile with flag `0x01` (ERC-20), amount = 100, and an Omni bridge message encoding an insufficient fee.
3. The ERC-20 contract burns 100 tokens and calls the precompile; the precompile schedules `ft_transfer_call(omni_bridge, 100, msg)` with a callback to `exit_to_near_precompile_callback`.
4. The Omni bridge receiver's `ft_on_transfer` returns `50` (rejects half due to insufficient fee).
5. `ft_resolve_transfer` refunds 50 NEP-141 tokens to Aurora's balance and returns `U128(50)` as the promise result.
6. `exit_to_near_precompile_callback` receives `PromiseResult::Successful(<50 encoded>)`, matches the success branch, and returns `None` — no re-mint occurs.
7. Final state: user has 0 ERC-20 tokens, received 50 NEP-141 tokens at the bridge destination, and 50 NEP-141 tokens are permanently frozen in Aurora's NEP-141 balance with no corresponding ERC-20 tokens to burn to recover them. [7](#0-6) [8](#0-7)

### Citations

**File:** engine-precompiles/src/native.rs (L436-447)
```rust
                // This precompile branch is expected to be called from the ERC-20 burn function.
                //
                // Input slice format:
                //  amount (U256 big-endian bytes) - the amount that was burned
                //  recipient_account_id (bytes) - the NEAR recipient account which will receive
                //  NEP-141 tokens, or also can contain the `:unwrap` suffix in case of withdrawing
                //  wNEAR, or another message of JSON in case of OMNI, or address of receiver in case
                //  of transfer tokens to another engine contract.
                ExitToNearParams::Erc20TokenParams(ref exit_params) => {
                    exit_erc20_token_to_near(context, exit_params, &self.io)?
                }
            };
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
