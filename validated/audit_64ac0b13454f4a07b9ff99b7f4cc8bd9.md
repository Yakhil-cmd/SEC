### Title
Burned ERC-20/ETH Tokens Permanently Lost When Exit-to-NEAR Transfer Fails Without `error_refund` Feature - (`engine-precompiles/src/native.rs`)

---

### Summary

When the `error_refund` compile-time feature is not enabled, the `ExitToNear` precompile schedules the NEAR-side `ft_transfer` promise with **no callback** for regular ERC-20 and ETH exits. If that promise fails (e.g., recipient not registered with the NEP-141 contract), the ERC-20 tokens that were already burned on the EVM side — or the ETH already deducted from the sender's EVM balance — are permanently lost with no refund path.

---

### Finding Description

The `ExitToNear` precompile's `run()` method constructs `callback_args` as follows:

```rust
let callback_args = ExitToNearPrecompileCallbackArgs {
    #[cfg(feature = "error_refund")]
    refund: refund_call_args(&exit_to_near_params, &exit_event),
    #[cfg(not(feature = "error_refund"))]
    refund: None,          // always None when feature is disabled
    transfer_near: transfer_near_args,
};
```

For regular ERC-20 exits and ETH exits (non-wNEAR-unwrap), `transfer_near` is also `None`. This makes `callback_args` equal to `ExitToNearPrecompileCallbackArgs::default()`, triggering the branch:

```rust
let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
    PromiseArgs::Create(transfer_promise)   // no callback attached
} else {
    PromiseArgs::Callback(...)
};
```

A bare `PromiseArgs::Create` is scheduled — **no `exit_to_near_precompile_callback` is attached**. If the NEAR-side `ft_transfer` promise fails, there is no callback to detect the failure and re-mint the burned ERC-20 tokens or restore the ETH balance. The tokens are permanently destroyed.

The existing test suite explicitly acknowledges this outcome:

```rust
// If the refund feature is not enabled then there is no refund in the EVM
#[cfg(not(feature = "error_refund"))]
let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();
```

The `refund_on_error` function exists and correctly re-mints burned ERC-20 tokens or transfers ETH back from the precompile address, but it is only reachable through the callback path, which is never scheduled when `error_refund` is disabled.

---

### Impact Explanation

**Impact: Critical — Permanent freezing of funds.**

Any user who calls the `ExitToNear` precompile for a regular ERC-20 or ETH exit when the NEAR-side transfer fails will have their tokens permanently destroyed. The ERC-20 burn and ETH deduction are committed atomically within the EVM execution, but the NEAR-side transfer is asynchronous. Without a callback, there is no recovery path. The tokens cannot be recovered by any party.

---

### Likelihood Explanation

**Likelihood: High.**

The failure condition is easily triggered by any unprivileged user:
- Sending to a NEAR account that is not registered with the NEP-141 contract (the most common failure mode, as shown in `test_exit_to_near_refund`)
- Sending to an account with insufficient NEAR storage deposit
- Any transient NEAR-side failure

The entry path is fully user-controlled: a user calls an ERC-20 contract's `withdrawToNear` function, which calls the `ExitToNear` precompile. No special privileges are required.

---

### Recommendation

Enable the `error_refund` feature in the production build, or unconditionally attach the `exit_to_near_precompile_callback` for all exit paths (not just wNEAR-unwrap). The `refund_on_error` function already implements the correct recovery logic; it simply needs to be reachable on failure.

Specifically, `callback_args.refund` should always be populated with the sender address and amount so that `exit_to_near_precompile_callback` can re-mint burned tokens or restore ETH on any promise failure.

---

### Proof of Concept

The test `test_exit_to_near_refund` in `engine-tests/src/tests/erc20_connector.rs` directly demonstrates the loss:

1. User bridges NEP-141 tokens to Aurora as ERC-20.
2. User calls `withdrawToNear("unregistered.near", amount)` — the NEAR `ft_transfer` will fail because the recipient is not registered.
3. Without `error_refund`: ERC-20 balance is reduced by `FT_EXIT_AMOUNT` and never restored.
4. With `error_refund`: ERC-20 balance is fully restored via `refund_on_error`.

The test explicitly encodes this as two different expected outcomes gated on the feature flag. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** engine-precompiles/src/native.rs (L449-484)
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

**File:** engine-tests/src/tests/erc20_connector.rs (L656-665)
```rust
        #[cfg(feature = "error_refund")]
        let balance = FT_TRANSFER_AMOUNT.into();
        // If the refund feature is not enabled then there is no refund in the EVM
        #[cfg(not(feature = "error_refund"))]
        let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();

        assert_eq!(
            erc20_balance(&erc20, ft_owner_address, &aurora).await,
            balance
        );
```

**File:** engine-tests/src/tests/erc20_connector.rs (L771-780)
```rust
        #[cfg(feature = "error_refund")]
        let expected_balance = Wei::new_u64(INITIAL_ETH_BALANCE);
        // If the refund feature is not enabled, then there is no refund in the EVM
        #[cfg(not(feature = "error_refund"))]
        let expected_balance = Wei::new_u64(INITIAL_ETH_BALANCE - ETH_EXIT_AMOUNT);

        assert_eq!(
            eth_balance_of(signer_address, &aurora).await,
            expected_balance
        );
```
