### Title
Contract-Level Pause Blocks `exit_to_near_precompile_callback`, Causing Permanent Loss of In-Flight Bridge Funds - (`engine/src/contract_methods/connector.rs`)

---

### Summary

When a user calls the `ExitToNear` precompile to bridge ERC-20 tokens (or ETH) from Aurora to NEAR, the operation is split into two phases: (1) an EVM execution that burns the user's tokens and schedules a NEAR promise, and (2) a NEAR-level callback (`exit_to_near_precompile_callback`) that either completes the transfer or refunds the burned tokens on failure. If the Aurora Engine contract is paused via `pause_contract` between these two phases, the callback reverts due to `require_running`, the refund never executes, and the user's tokens are permanently lost.

---

### Finding Description

The `ExitToNear` precompile (`engine-precompiles/src/native.rs`) executes inside an EVM transaction. For the ERC-20 exit path, the ERC-20 contract burns the user's tokens and calls the precompile, which schedules a NEAR promise (`ft_transfer` or `near_withdraw`) and, when the `error_refund` feature is enabled or when unwrapping wNEAR, also schedules a callback to `exit_to_near_precompile_callback` on the Aurora contract itself. [1](#0-0) 

The callback is responsible for either completing the NEAR token transfer (wNEAR unwrap case) or re-minting the burned ERC-20 tokens if the NEAR-side promise failed (refund case): [2](#0-1) 

Critically, `exit_to_near_precompile_callback` calls `require_running` at line 203 before doing anything: [3](#0-2) 

`require_running` is defined as: [4](#0-3) 

The contract-level pause is set by `pause_contract`: [5](#0-4) 

The two pause mechanisms — `pause_contract` (contract-wide) and `pause_precompiles` (precompile-specific bitmask) — are completely independent: [6](#0-5) 

When the precompile is paused via `pause_precompiles`, the EVM transaction itself fails with `ERR_PAUSED` (a fatal EVM error), so the ERC-20 burn never occurs — this is safe: [7](#0-6) 

But when the **contract** is paused via `pause_contract`, new EVM transactions are blocked, yet **in-flight NEAR callbacks** that were already scheduled before the pause still arrive. These callbacks hit `require_running` and revert. The refund path (`engine::refund_on_error`) and the wNEAR NEAR-transfer path are both gated behind this check and never execute.

The refund path (when `error_refund` feature is enabled): [8](#0-7) 

The wNEAR NEAR-transfer path (always present when unwrapping wNEAR): [9](#0-8) 

The refund logic itself (re-minting burned ERC-20 or returning ETH from the precompile address): [10](#0-9) 

---

### Impact Explanation

Two concrete loss scenarios:

**Scenario A — ERC-20 exit with `error_refund` enabled**: A user's ERC-20 tokens are burned in the EVM. The `ft_transfer` to the NEP-141 contract fails (e.g., recipient not registered). The callback arrives while the contract is paused. `require_running` reverts the callback. The re-mint (`refund_on_error`) never runs. The user's ERC-20 tokens are permanently destroyed with no corresponding NEP-141 credit.

**Scenario B — wNEAR unwrap**: A user burns wNEAR ERC-20 tokens and calls `near_withdraw` on the wNEAR contract. The NEAR is successfully unwrapped and held by Aurora. The callback arrives while the contract is paused. `require_running` reverts. The NEAR transfer to the user's account never happens. The NEAR is permanently frozen inside Aurora with no way to recover it.

Both scenarios result in **permanent loss or freeze of user funds** — Critical severity.

---

### Likelihood Explanation

The window between the EVM transaction and the NEAR callback is typically one NEAR block (~1 second). However, a contract pause during a maintenance window is a realistic operational event. Any in-flight `exit_to_near_precompile_callback` receipts that were queued before the pause and execute after it will silently fail. The user has no mechanism to replay or recover the funds. The likelihood is **medium** — it requires a contract pause to coincide with in-flight callbacks, which is plausible during routine maintenance.

---

### Recommendation

1. Remove `require_running` from `exit_to_near_precompile_callback`. This callback is a private self-call (`env.assert_private_call()` is already enforced) and is not a user-facing mutative entry point — it is the completion leg of an already-committed EVM operation. Blocking it causes irreversible fund loss.

2. Alternatively, drain all in-flight exit callbacks before allowing `pause_contract` to take effect, or track pending callbacks in storage so they can be replayed after the contract is resumed.

3. Document clearly that `pause_contract` must not be called while there are pending `exit_to_near_precompile_callback` receipts in the NEAR receipt queue.

---

### Proof of Concept

1. User submits an EVM transaction that calls the `ExitToNear` precompile with the wNEAR ERC-20 token and the `:unwrap` suffix. The wNEAR ERC-20 tokens are burned. A `near_withdraw` promise + `exit_to_near_precompile_callback` callback are scheduled.
2. Before the callback executes (within the same or next NEAR block), the Aurora owner calls `pause_contract`.
3. The `near_withdraw` promise executes on the wNEAR contract — succeeds, NEAR is now held by Aurora.
4. `exit_to_near_precompile_callback` executes. Line 203 calls `require_running` → returns `Err(ERR_PAUSED)` → the callback panics/reverts.
5. The NEAR transfer to the user (`PromiseAction::Transfer`) never happens.
6. The user has lost their wNEAR ERC-20 tokens (burned in step 1) and the unwrapped NEAR is permanently frozen in Aurora with no recovery path.

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

**File:** engine/src/contract_methods/mod.rs (L65-70)
```rust
pub fn require_running(state: &state::EngineState) -> Result<(), ContractError> {
    if state.is_paused {
        return Err(errors::ERR_PAUSED.into());
    }
    Ok(())
}
```

**File:** engine/src/contract_methods/admin.rs (L250-260)
```rust
#[named]
pub fn pause_contract<I: IO + Copy, E: Env>(io: I, env: &E) -> Result<(), ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        let mut state = state::get_state(&io)?;
        require_owner_only(&state, &env.predecessor_account_id())?;
        require_running(&state)?;
        state.is_paused = true;
        state::set_state(&mut io, &state)?;
        Ok(())
    })
}
```

**File:** engine/src/pausables.rs (L9-17)
```rust
bitflags! {
    /// Wraps unsigned integer where each bit identifies a different precompile.
    #[derive(BorshSerialize, BorshDeserialize, Default)]
    #[borsh(crate = "aurora_engine_types::borsh")]
    pub struct PrecompileFlags: u32 {
        const EXIT_TO_NEAR        = 0b01;
        const EXIT_TO_ETHEREUM    = 0b10;
    }
}
```

**File:** engine-precompiles/src/lib.rs (L140-144)
```rust
        if self.is_paused(&address) {
            return Some(Err(PrecompileFailure::Fatal {
                exit_status: ExitFatal::Other(prelude::Cow::Borrowed("ERR_PAUSED")),
            }));
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
