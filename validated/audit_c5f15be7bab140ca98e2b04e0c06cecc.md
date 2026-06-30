### Title
ETH Permanently Locked at `ExitToNear` Precompile Address When `ft_transfer` Fails Without `error_refund` Feature - (File: engine-precompiles/src/native.rs)

### Summary
When the `error_refund` compile-time feature is not enabled, the `ExitToNear` precompile deducts ETH from the user's EVM balance synchronously but schedules the NEP-141 `ft_transfer` asynchronously with no failure callback. If the `ft_transfer` promise fails, the ETH is permanently locked at the precompile address with no recovery path, creating a permanent accounting inconsistency between EVM state and NEAR state.

### Finding Description
The `ExitToNear` precompile (`exit_to_near::ADDRESS`) handles ETH withdrawals to NEAR in two distinct phases:

**Phase 1 (synchronous, EVM):** When a user sends ETH to the precompile, the EVM immediately deducts `context.apparent_value` from the caller's balance and credits it to the precompile address. This is irreversible within the EVM execution.

**Phase 2 (asynchronous, NEAR):** The precompile schedules an `ft_transfer` promise on the ETH connector to transfer the corresponding NEP-141 tokens to the recipient.

The critical inconsistency is in how the callback is constructed:

```rust
// engine-precompiles/src/native.rs
let callback_args = ExitToNearPrecompileCallbackArgs {
    #[cfg(feature = "error_refund")]
    refund: refund_call_args(&exit_to_near_params, &exit_event),
    #[cfg(not(feature = "error_refund"))]
    refund: None,          // <-- no refund args when feature is absent
    transfer_near: transfer_near_args,
};

let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
    PromiseArgs::Create(transfer_promise)  // <-- no callback scheduled at all
} else {
    PromiseArgs::Callback(PromiseWithCallbackArgs { ... })
};
``` [1](#0-0) 

When `error_refund` is absent and `transfer_near` is also `None` (the common ETH exit case), `callback_args` equals `default()`, so only a bare `PromiseArgs::Create` is emitted — **no callback is ever scheduled**. If `ft_transfer` fails (e.g., recipient not registered with the NEP-141 contract), there is no mechanism to detect the failure or return the ETH.

The `refund_on_error` function that would restore ETH from the precompile address to the user is only reachable through `exit_to_near_precompile_callback`, which is never scheduled in this path:

```rust
// engine/src/engine.rs
} else {
    // ETH exit; transfer ETH back from precompile address
    let exit_address = exit_to_near::ADDRESS;
    ...
    engine.call(&exit_address, &refund_address, amount, ...)
}
``` [2](#0-1) 

The callback function itself confirms the refund only fires when `args.refund` is `Some`:

```rust
// engine/src/contract_methods/connector.rs
} else if let Some(args) = args.refund {
    let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;
    ...
} else {
    None  // <-- silent no-op when refund is None
}
``` [3](#0-2) 

The test suite explicitly acknowledges this behavior:

```rust
// If the refund feature is not enabled, then there is no refund in the EVM
#[cfg(not(feature = "error_refund"))]
let expected_balance = Wei::new_u64(INITIAL_ETH_BALANCE - ETH_EXIT_AMOUNT);
``` [4](#0-3) 

The same pattern applies to ERC-20 exits: tokens are burned from the EVM synchronously, but if the NEP-141 `ft_transfer` fails without `error_refund`, the ERC-20 tokens are permanently destroyed with no NEP-141 transfer completing.

### Impact Explanation
**Critical — Permanent freezing of funds.** ETH (or ERC-20 tokens) sent to the `ExitToNear` precompile is deducted from the user's EVM balance immediately and irrevocably. If the asynchronous `ft_transfer` fails and `error_refund` is not compiled in, the ETH is permanently locked at `exit_to_near::ADDRESS` with no admin function, no recovery path, and no way for the user to reclaim it. The EVM total supply decreases while the NEP-141 total supply held by Aurora does not, creating a permanent 1:1 peg break between EVM ETH and NEP-141 ETH.

### Likelihood Explanation
**High.** Any EVM user can trigger this by calling `withdrawEthToNear` (or `withdrawToNear` on an ERC-20) with a recipient NEAR account that is not registered with the NEP-141/ETH connector contract. This is a realistic user mistake — a user may not know whether a given NEAR account has called `storage_deposit` on the connector. No special privileges are required; the attacker-controlled entry path is a standard EVM transaction to any contract that calls the `ExitToNear` precompile.

### Recommendation
Ensure the `error_refund` feature is always enabled in production builds, or make the refund callback unconditional (not gated behind a feature flag). At minimum, if `ft_transfer` fails and no refund callback is scheduled, the EVM transaction itself should revert so the user's ETH is never deducted. The accounting between EVM ETH balance deduction and NEP-141 transfer must be atomic or fully reversible.

### Proof of Concept
1. Deploy Aurora Engine **without** the `error_refund` feature.
2. Fund an EVM address with ETH (e.g., via `mint_account`).
3. Ensure the ETH connector has sufficient NEP-141 balance for Aurora's account.
4. Call `withdrawEthToNear` targeting a NEAR account that has **not** called `storage_deposit` on the ETH connector (e.g., a freshly created account).
5. The EVM transaction succeeds: ETH is deducted from the user's EVM balance.
6. The `ft_transfer` promise fails: the recipient is not registered.
7. No callback fires; no refund occurs.
8. Observe: `eth_balance_of(user) == INITIAL - EXIT_AMOUNT` (ETH gone), `nep141_balance_of(recipient) == 0` (no transfer), ETH permanently locked at `exit_to_near::ADDRESS`. [5](#0-4) [6](#0-5)

### Citations

**File:** engine-precompiles/src/native.rs (L449-501)
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
        let promise_log = Log {
            address: exit_to_near::ADDRESS.raw(),
            topics: Vec::new(),
            data: borsh::to_vec(&promise).unwrap(),
        };
        let ethabi::RawLog { topics, data } = exit_event.encode();
        let exit_event_log = Log {
            address: exit_to_near::ADDRESS.raw(),
            topics: topics.into_iter().map(|h| H256::from(h.0)).collect(),
            data,
        };

        Ok(PrecompileOutput {
            logs: vec![promise_log, exit_event_log],
            cost: required_gas,
            output: Vec::new(),
        })
    }
```

**File:** engine/src/engine.rs (L1204-1224)
```rust
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

**File:** engine/src/contract_methods/connector.rs (L196-245)
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
```

**File:** engine-tests/src/tests/erc20_connector.rs (L773-775)
```rust
        // If the refund feature is not enabled, then there is no refund in the EVM
        #[cfg(not(feature = "error_refund"))]
        let expected_balance = Wei::new_u64(INITIAL_ETH_BALANCE - ETH_EXIT_AMOUNT);
```
