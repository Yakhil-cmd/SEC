### Title
Permanent Fund Freeze When `ft_transfer` Fails in `ExitToNear` Precompile Without `error_refund` Feature — (`engine-precompiles/src/native.rs`)

---

### Summary

When the `error_refund` compile-time feature is absent, a failed `ft_transfer` or `ft_transfer_call` promise in the `ExitToNear` precompile flow leaves ERC-20 tokens permanently burned and the corresponding NEP-141 tokens permanently frozen inside the Aurora Engine contract. No recovery path exists in the contract logic for this configuration.

---

### Finding Description

The `ExitToNear` precompile handles two token exit paths: ETH (base token) and ERC-20 (bridged NEP-141). In both cases the flow is:

1. ERC-20 tokens are **burned** from the user's EVM balance (or ETH is moved to the precompile address).
2. A NEAR promise is created to call `ft_transfer` / `ft_transfer_call` on the NEP-141 contract.
3. A callback `exit_to_near_precompile_callback` is optionally attached.

The callback's `refund` field is populated only when the `error_refund` feature is compiled in:

```rust
// engine-precompiles/src/native.rs  lines 449-453
let callback_args = ExitToNearPrecompileCallbackArgs {
    #[cfg(feature = "error_refund")]
    refund: refund_call_args(&exit_to_near_params, &exit_event),
    #[cfg(not(feature = "error_refund"))]
    refund: None,          // ← always None without the feature
    transfer_near: transfer_near_args,
};
``` [1](#0-0) 

When the `ft_transfer` promise fails (e.g., the recipient NEAR account is not registered with the NEP-141 token), `exit_to_near_precompile_callback` is invoked. The callback checks `args.refund`:

```rust
// engine/src/contract_methods/connector.rs  lines 231-241
} else if let Some(args) = args.refund {
    // Exit call failed; need to refund tokens
    let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;
    ...
} else {
    None   // ← reached when refund is None; nothing happens
};
``` [2](#0-1) 

Because `refund` is `None`, `refund_on_error` is never called. For ERC-20 exits this means:
- The ERC-20 tokens are **permanently burned** from the user's EVM balance.
- The NEP-141 tokens remain in Aurora's account with no mechanism to retrieve them.

For ETH exits, the ETH is transferred to the `exit_to_near` precompile address and similarly has no recovery path.

The `refund_on_error` function that would re-mint ERC-20 tokens or return ETH exists but is unreachable in this configuration:

```rust
// engine/src/engine.rs  lines 1176-1224
pub fn refund_on_error<I: IO + Copy, E: Env, P: PromiseHandler>(
    io: I, env: &E, state: EngineState, args: &RefundCallArgs, handler: &mut P,
) -> EngineResult<SubmitResult> {
    if let Some(erc20_address) = args.erc20_address {
        // ERC-20 exit; re-mint burned tokens
        ...
    } else {
        // ETH exit; transfer ETH back from precompile address
        ...
    }
}
``` [3](#0-2) 

The test suite explicitly acknowledges this behavior:

```rust
// engine-tests/src/tests/erc20_connector.rs  lines 656-660
#[cfg(feature = "error_refund")]
let balance = FT_TRANSFER_AMOUNT.into();
// If the refund feature is not enabled then there is no refund in the EVM
#[cfg(not(feature = "error_refund"))]
let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();
``` [4](#0-3) 

---

### Impact Explanation

**Permanent freezing of funds.** When `error_refund` is not compiled in and `ft_transfer` fails:

- **ERC-20 path**: The user's ERC-20 tokens are permanently burned. The corresponding NEP-141 tokens remain locked inside Aurora's account with no on-chain withdrawal path.
- **ETH path**: The user's ETH is permanently transferred to the `exit_to_near` precompile address and cannot be recovered.

There is no queue, no admin-callable recovery function, and no on-chain mechanism to unfreeze these funds within the current contract logic. The only recovery would be a contract upgrade — analogous to the Andromeda finding.

---

### Likelihood Explanation

Any EVM user holding bridged ERC-20 tokens (or ETH) can trigger this by calling the exit precompile with a NEAR recipient account that is not registered with the NEP-141 token. This is a realistic scenario: users may mistype account IDs, target accounts that have not called `storage_deposit`, or target accounts on a different shard. The `ft_transfer` call will fail silently from the EVM's perspective (the EVM transaction succeeds), and the funds are gone.

The `ExitToNear` precompile is a core, publicly reachable interface — any EVM user can call it via a standard EVM transaction submitted through `submit`. [5](#0-4) 

---

### Recommendation

1. **Enable `error_refund` unconditionally** in the production build, or promote it to a default feature in `Cargo.toml` so it cannot be accidentally omitted.
2. Alternatively, remove the conditional compilation and always populate `refund` in `ExitToNearPrecompileCallbackArgs`, ensuring `refund_on_error` is always reachable on failure.
3. Add a guard in `exit_to_near_precompile_callback` that panics or returns an error if `refund` is `None` and the base promise failed, preventing silent fund loss.

---

### Proof of Concept

1. Deploy Aurora Engine **without** the `error_refund` feature.
2. Bridge a NEP-141 token to Aurora, receiving ERC-20 tokens at address `user_evm`.
3. From `user_evm`, call the ERC-20's exit function targeting `unregistered.near` (an account that has never called `storage_deposit` on the NEP-141 contract).
4. The ERC-20 tokens are burned from `user_evm`'s balance.
5. The `ft_transfer` promise to `unregistered.near` fails.
6. `exit_to_near_precompile_callback` is invoked with `refund: None`.
7. The callback returns `None` — no re-mint, no recovery.
8. Observe: `user_evm` ERC-20 balance = 0; NEP-141 balance of Aurora account is unchanged (tokens remain inside Aurora); `unregistered.near` NEP-141 balance = 0.

The funds are permanently frozen. This matches the test assertion at `engine-tests/src/tests/erc20_connector.rs:659` which confirms the balance deficit when `error_refund` is absent. [6](#0-5) [7](#0-6) [3](#0-2)

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

**File:** engine/src/contract_methods/connector.rs (L195-245)
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

**File:** engine-tests/src/tests/erc20_connector.rs (L656-660)
```rust
        #[cfg(feature = "error_refund")]
        let balance = FT_TRANSFER_AMOUNT.into();
        // If the refund feature is not enabled then there is no refund in the EVM
        #[cfg(not(feature = "error_refund"))]
        let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();
```

**File:** engine/src/lib.rs (L602-610)
```rust
    #[unsafe(no_mangle)]
    pub extern "C" fn ft_on_transfer() {
        let io = Runtime;
        let env = Runtime;
        let mut handler = Runtime;
        contract_methods::connector::ft_on_transfer(io, &env, &mut handler)
            .map_err(ContractError::msg)
            .sdk_unwrap();
    }
```
