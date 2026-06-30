### Title
Permanent ERC-20 Token Loss When `ExitToNear` NEP-141 Transfer Fails Without `error_refund` Feature — (`engine-precompiles/src/native.rs`)

---

### Summary

When the `error_refund` compile-time feature is absent, ERC-20 tokens burned inside the `ExitToNear` precompile are permanently destroyed if the subsequent NEP-141 `ft_transfer` promise fails. No callback is attached to handle the failure, and no refund path exists in the EVM. Any token holder can trigger this by supplying a recipient account that is not registered with the NEP-141 contract.

---

### Finding Description

The `ExitToNear` precompile (`engine-precompiles/src/native.rs`) handles ERC-20-to-NEAR exits in two compile-time variants controlled by the `error_refund` feature flag.

**Step 1 — Callback args construction:** [1](#0-0) 

When `error_refund` is **not** compiled in, `refund` is unconditionally `None`:

```rust
let callback_args = ExitToNearPrecompileCallbackArgs {
    #[cfg(feature = "error_refund")]
    refund: refund_call_args(&exit_to_near_params, &exit_event),
    #[cfg(not(feature = "error_refund"))]
    refund: None,          // ← always None without the feature
    transfer_near: transfer_near_args,
};
```

**Step 2 — Promise construction (no callback attached for plain ERC-20 exits):** [2](#0-1) 

For a regular ERC-20 exit (no wNEAR unwrap), `transfer_near_args` is also `None`, so `callback_args` equals the default value. The branch therefore emits a bare `PromiseArgs::Create` — **no callback is attached at all**:

```rust
let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
    PromiseArgs::Create(transfer_promise)   // ← no error-handling callback
} else {
    PromiseArgs::Callback(...)
};
```

**Step 3 — Callback handler ignores `None` refund:**

Even in the wNEAR-unwrap path where a callback *is* attached, the callback handler only refunds when `args.refund` is `Some`: [3](#0-2) 

```rust
} else if let Some(args) = args.refund {
    // Exit call failed; need to refund tokens
    let refund_result = engine::refund_on_error(...)?;
    ...
} else {
    None   // ← silent no-op when refund is None
}
```

**Step 4 — ERC-20 tokens are already burned before the promise executes:**

The ERC-20 `withdraw` function burns the caller's tokens atomically inside the EVM transaction. The NEAR promise is only *scheduled* at that point. If the promise fails, the EVM state is not rolled back — the burn is final.

**The test explicitly documents this loss:** [4](#0-3) 

```rust
#[cfg(feature = "error_refund")]
let balance = FT_TRANSFER_AMOUNT.into();
// If the refund feature is not enabled then there is no refund in the EVM
#[cfg(not(feature = "error_refund"))]
let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();
```

The balance is permanently reduced by `FT_EXIT_AMOUNT` even though the NEP-141 transfer failed.

---

### Impact Explanation

**Permanent freezing / destruction of funds.** Any ERC-20 token holder who calls `ExitToNear` with a recipient account that is not registered with the NEP-141 contract (or whose registration lapses, or who makes a typo) will have their ERC-20 tokens burned with zero recovery path. The NEP-141 tokens never arrive at the destination. The funds are gone from both sides of the bridge.

This maps directly to the allowed impact scope: **Critical — Permanent freezing of funds.**

---

### Likelihood Explanation

- **Reachable by any unprivileged ERC-20 token holder** — no special role required.
- **Trigger condition is ordinary user error**: supplying an unregistered NEAR account ID (e.g., a typo, a freshly created account that has not yet called `storage_deposit` on the NEP-141 contract, or an account that unregistered after the user initiated the transaction).
- The NEP-141 standard requires explicit storage registration; many accounts are not registered by default.
- The `error_refund` feature is opt-in at compile time. Any production build that omits it is fully exposed.

---

### Recommendation

1. **Make the refund path unconditional.** Remove the `#[cfg(feature = "error_refund")]` guard around `refund_call_args` and always attach the callback. The callback already handles the `refund: None` case gracefully (it is a no-op), so enabling it unconditionally adds no overhead for the success path.

2. **Always attach the callback for ERC-20 exits**, regardless of whether `transfer_near_args` is `None`. The current short-circuit (`if callback_args == default()`) silently drops error handling.

3. **Ensure `error_refund` is a default feature** in `engine/Cargo.toml` so it cannot be accidentally omitted in a production build.

---

### Proof of Concept

1. Deploy a NEP-141 token; bridge it to Aurora as an ERC-20.
2. As an ERC-20 token holder, call the ERC-20's `withdraw` function targeting `"unregistered.near"` (any account that has not called `storage_deposit` on the NEP-141 contract).
3. The EVM transaction succeeds: ERC-20 tokens are burned, `ExitToNear` precompile schedules a bare `ft_transfer` promise with no callback.
4. The `ft_transfer` promise fails on NEAR (unregistered recipient).
5. No callback fires; no refund is issued.
6. Observe: ERC-20 balance reduced by exit amount; NEP-141 balance of `"unregistered.near"` remains 0; tokens are permanently destroyed.

This is confirmed by the existing test `test_exit_to_near_refund` in `engine-tests/src/tests/erc20_connector.rs` (lines 623–665), which explicitly asserts the token loss when compiled without `error_refund`. [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7)

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

**File:** engine-tests/src/tests/erc20_connector.rs (L623-665)
```rust
    #[tokio::test]
    async fn test_exit_to_near_refund() {
        // Deploy Aurora; deploy NEP-141; bridge NEP-141 to ERC-20 on Aurora
        let TestExitToNearContext {
            ft_owner,
            ft_owner_address,
            nep_141,
            erc20,
            aurora,
            ..
        } = test_exit_to_near_common().await.unwrap();

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

        assert_eq!(
            nep_141_balance_of(&nep_141, &ft_owner.id()).await,
            FT_TOTAL_SUPPLY - FT_TRANSFER_AMOUNT
        );
        assert_eq!(
            nep_141_balance_of(&nep_141, &aurora.id()).await,
            FT_TRANSFER_AMOUNT
        );

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
