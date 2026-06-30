### Title
Permanent ERC-20 Token Loss via Malicious `ft_on_transfer` Callback in `ExitToNear` Omni Path — (`engine-precompiles/src/native.rs`)

---

### Summary

The `ExitToNear` precompile burns ERC-20 tokens on Aurora **before** the corresponding NEAR-side `ft_transfer_call` promise executes. When the `error_refund` compile-time feature is disabled, no refund callback is attached. A malicious receiver NEAR contract can deliberately revert its `ft_on_transfer` callback, causing the NEP-141 tokens to be returned to Aurora's account while the user's ERC-20 tokens are permanently destroyed with no recovery path.

---

### Finding Description

The `ExitToNear` precompile supports an "Omni" exit path where a user bridges ERC-20 tokens to NEAR using `ft_transfer_call` (triggered by appending a `:msg` suffix to the recipient). The flow is:

1. The ERC-20 contract calls the `ExitToNear` precompile, which records the burn.
2. The precompile schedules a NEAR promise calling `ft_transfer_call` on the NEP-141 contract.
3. The NEP-141 standard calls `ft_on_transfer` on the receiver contract.
4. If the receiver reverts `ft_on_transfer`, the NEP-141 standard returns the tokens to Aurora's account — but the ERC-20 tokens on Aurora are already burned.

The critical decision point is in `ExitToNear::run`:

```rust
let callback_args = ExitToNearPrecompileCallbackArgs {
    #[cfg(feature = "error_refund")]
    refund: refund_call_args(&exit_to_near_params, &exit_event),
    #[cfg(not(feature = "error_refund"))]
    refund: None,
    transfer_near: transfer_near_args, // None for Omni ERC-20 path
};

let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
    PromiseArgs::Create(transfer_promise)   // ← no callback attached
} else {
    PromiseArgs::Callback(PromiseWithCallbackArgs { ... }) // ← refund callback
};
``` [1](#0-0) 

Without `error_refund`, `callback_args = { refund: None, transfer_near: None }` equals the default, so **no callback is attached**. The `exit_to_near_precompile_callback` that would re-mint burned tokens is never scheduled. [2](#0-1) 

The refund logic that would recover tokens only executes inside `exit_to_near_precompile_callback`:

```rust
} else if let Some(args) = args.refund {
    // Exit call failed; need to refund tokens
    let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;
``` [3](#0-2) 

The `refund_on_error` function re-mints burned ERC-20 tokens or restores ETH balance, but it is only reachable when the callback is attached: [4](#0-3) 

The test suite explicitly acknowledges this permanent-loss behavior when `error_refund` is absent:

```rust
// If the refund feature is not enabled then there is no refund in the EVM
#[cfg(not(feature = "error_refund"))]
let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();
``` [5](#0-4) 

---

### Impact Explanation

**Permanent freezing of funds.** When `error_refund` is not enabled:

- The user's ERC-20 tokens are burned on Aurora (irreversible within the EVM).
- The NEP-141 tokens are returned to Aurora's NEAR account (not the user's).
- The user ends up with neither ERC-20 tokens nor NEP-141 tokens.
- There is no admin function or recovery path to restore the burned ERC-20 tokens.

This affects all three non-wNEAR exit paths: legacy ERC-20 (`ft_transfer`), Omni ERC-20 (`ft_transfer_call`), and ETH base token exits.

---

### Likelihood Explanation

**Medium.** Two conditions must hold:

1. The `error_refund` feature is not enabled in the production build. The feature is optional and gated by `#[cfg(feature = "error_refund")]` throughout the codebase, meaning deployments without it are plausible.
2. For the attacker-controlled variant (direct analog to M-07): a user initiates an Omni exit to a receiver NEAR contract that deliberately reverts `ft_on_transfer`. The receiver has zero cost — they never receive tokens and lose nothing by reverting. For the non-attacker variant: any failed `ft_transfer` (e.g., unregistered recipient) also causes permanent loss. [6](#0-5) 

---

### Recommendation

1. **Make the refund callback unconditional**: Remove the `#[cfg(feature = "error_refund")]` gate on `refund_call_args`. The callback should always be attached for ERC-20 and ETH exits so that any failure on the NEAR side triggers a re-mint.
2. **Alternatively**: Ensure `error_refund` is always enabled in production builds and document this as a required feature.
3. **For the Omni `ft_transfer_call` path specifically**: Consider separating the burn step from the promise scheduling, or using a two-phase commit pattern where the burn is only finalized after the NEAR-side transfer succeeds.

---

### Proof of Concept

1. Deploy a malicious NEAR contract `evil.near` that implements `ft_on_transfer` and always panics.
2. Bridge an ERC-20 token (backed by a NEP-141) to Aurora.
3. Call `withdrawToNear` on the ERC-20 contract with destination `"evil.near:some_msg"` (Omni path).
4. The ERC-20 precompile burns the tokens and schedules `ft_transfer_call` targeting `evil.near`.
5. `evil.near`'s `ft_on_transfer` panics; NEP-141 returns tokens to Aurora's account.
6. Without `error_refund`, no callback fires. The user's ERC-20 balance is zero and the NEP-141 tokens sit in Aurora's account, inaccessible to the user — permanent fund freeze. [7](#0-6) [8](#0-7)

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

**File:** engine-tests/src/tests/erc20_connector.rs (L656-660)
```rust
        #[cfg(feature = "error_refund")]
        let balance = FT_TRANSFER_AMOUNT.into();
        // If the refund feature is not enabled then there is no refund in the EVM
        #[cfg(not(feature = "error_refund"))]
        let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();
```
