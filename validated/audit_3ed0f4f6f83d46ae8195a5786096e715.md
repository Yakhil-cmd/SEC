### Title
Permanent Fund Freeze When `error_refund` Feature Is Disabled and NEAR-Side `ft_transfer` Fails in `ExitToNear` Precompile - (`engine-precompiles/src/native.rs`)

### Summary

The `ExitToNear` precompile burns/debits EVM-side tokens (ETH or ERC-20) before scheduling a NEAR-side `ft_transfer` promise. When the compile-time feature `error_refund` is **not** enabled, the precompile sets `refund: None` in the callback args, causing the promise to be created with **no callback**. If the NEAR-side `ft_transfer` fails (e.g., the recipient account is not registered with the NEP-141 token), the user's EVM tokens are permanently lost with no recovery path.

### Finding Description

In `engine-precompiles/src/native.rs`, the `ExitToNear` precompile constructs callback arguments as follows:

```rust
let callback_args = ExitToNearPrecompileCallbackArgs {
    #[cfg(feature = "error_refund")]
    refund: refund_call_args(&exit_to_near_params, &exit_event),
    #[cfg(not(feature = "error_refund"))]
    refund: None,          // ← no refund args when feature is off
    transfer_near: transfer_near_args,
};
``` [1](#0-0) 

Then the promise is created conditionally:

```rust
let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
    PromiseArgs::Create(transfer_promise)   // ← no callback attached
} else {
    PromiseArgs::Callback(PromiseWithCallbackArgs { ... })
};
``` [2](#0-1) 

For the standard ETH base-token exit and regular ERC-20 exit (non-wNEAR-unwrap), `transfer_near` is `None`. When `error_refund` is also disabled, `callback_args` equals the default (both fields `None`), so the promise is created **without any callback**. The NEAR-side `ft_transfer` failure is silently swallowed.

The `refund_on_error` function in `engine/src/engine.rs` — which would re-mint burned ERC-20 tokens or transfer ETH back from the precompile address — is only reachable through `exit_to_near_precompile_callback`, which is only scheduled when `callback_args != default()`. [3](#0-2) 

The callback handler `exit_to_near_precompile_callback` in `engine/src/contract_methods/connector.rs` only executes the refund path when `args.refund` is `Some(...)`:

```rust
} else if let Some(args) = args.refund {
    // Exit call failed; need to refund tokens
    let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;
``` [4](#0-3) 

The test suite explicitly acknowledges this behavior:

```rust
// If the refund feature is not enabled then there is no refund in the EVM
#[cfg(not(feature = "error_refund"))]
let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();
``` [5](#0-4) 

And for ETH:

```rust
// If the refund feature is not enabled, then there is no refund in the EVM
#[cfg(not(feature = "error_refund"))]
let expected_balance = Wei::new_u64(INITIAL_ETH_BALANCE - ETH_EXIT_AMOUNT);
``` [6](#0-5) 

### Impact Explanation

When `error_refund` is not compiled in:

- **ETH exit**: The user's EVM ETH balance is debited before the NEAR `ft_transfer` is attempted. If `ft_transfer` fails, the ETH is gone from the EVM and never arrives on NEAR — **permanent fund freeze**.
- **ERC-20 exit**: The ERC-20 tokens are burned from the user's EVM balance before the NEAR `ft_transfer` is attempted. If `ft_transfer` fails, the tokens are gone — **permanent fund freeze**.

This matches the Critical impact tier: permanent freezing of user funds.

### Likelihood Explanation

The `ft_transfer` promise fails whenever the NEAR recipient account is not registered with the NEP-141 token (storage deposit not paid). This is a common, easily triggered condition — any user who specifies a NEAR account that has not pre-registered with the specific NEP-141 contract will trigger the failure. No special attacker capability is needed; an ordinary EVM user making a routine bridge-out call with an unregistered recipient account is sufficient. [7](#0-6) 

### Recommendation

Ensure the `error_refund` feature is always enabled in production builds, or restructure the refund logic so it does not depend on a compile-time feature flag. The refund callback should be unconditionally attached whenever EVM-side tokens are debited, regardless of the `error_refund` feature state. Alternatively, adopt a "check-then-debit" pattern: only burn/debit EVM tokens after the NEAR-side transfer has been confirmed successful.

### Proof of Concept

1. Deploy Aurora Engine **without** the `error_refund` feature.
2. Fund an EVM address with ETH or an ERC-20 token.
3. Call the `ExitToNear` precompile (`0xe9217bc70b7ed1f598ddd3199e80b093fa71124f`) specifying a NEAR recipient account that has **not** registered storage with the NEP-141 token.
4. The EVM-side balance is debited immediately (ETH burned or ERC-20 burned).
5. The NEAR `ft_transfer` promise fails (unregistered account).
6. No callback is scheduled; no refund occurs.
7. The user's tokens are permanently lost — confirmed by the test `test_exit_to_near_eth_refund` and `test_exit_to_near_refund` which show `expected_balance = INITIAL_ETH_BALANCE - ETH_EXIT_AMOUNT` when `error_refund` is off. [8](#0-7) [9](#0-8) [10](#0-9)

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

**File:** engine/src/engine.rs (L1176-1225)
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
}
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

**File:** engine-tests/src/tests/erc20_connector.rs (L635-645)
```rust
        // Call exit on ERC-20; ft_transfer promise fails; expect refund on Aurora;
        exit_to_near(
            &ft_owner,
            // The ft_transfer will fail because this account is not registered with the NEP-141
            "unregistered.near",
            FT_EXIT_AMOUNT,
            &erc20,
            &aurora,
        )
        .await
        .unwrap();
```

**File:** engine-tests/src/tests/erc20_connector.rs (L658-660)
```rust
        // If the refund feature is not enabled then there is no refund in the EVM
        #[cfg(not(feature = "error_refund"))]
        let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();
```

**File:** engine-tests/src/tests/erc20_connector.rs (L773-775)
```rust
        // If the refund feature is not enabled, then there is no refund in the EVM
        #[cfg(not(feature = "error_refund"))]
        let expected_balance = Wei::new_u64(INITIAL_ETH_BALANCE - ETH_EXIT_AMOUNT);
```
