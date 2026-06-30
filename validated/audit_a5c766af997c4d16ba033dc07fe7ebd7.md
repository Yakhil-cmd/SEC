### Title
Incomplete Refund Path in `exit_to_near_precompile_callback` When `error_refund` Feature Is Absent — (`File: engine/src/contract_methods/connector.rs` + `engine-precompiles/src/native.rs`)

---

### Summary

When a user calls the `exitToNear` precompile (either for ERC-20 tokens or base ETH), the EVM-side tokens are burned/transferred to the precompile address **before** the NEAR-side `ft_transfer` promise is dispatched. If that promise fails, the `exit_to_near_precompile_callback` is invoked. However, the refund argument (`args.refund`) is **hardcoded to `None`** when the `error_refund` Cargo feature is not compiled in. The callback then silently falls through to the `else { None }` branch, performing no refund. The user's ERC-20 tokens are permanently burned, or their ETH is permanently locked in the `exit_to_near` precompile address, with no recovery path.

---

### Finding Description

**Step 1 — Tokens are burned/locked before the cross-chain promise resolves.**

In `exit_erc20_token_to_near`, the ERC-20 contract calls the precompile, which burns the tokens on the EVM side and schedules an `ft_transfer` on the NEP-141 contract. In `exit_base_token_to_near`, ETH is transferred to `exit_to_near::ADDRESS`. Both happen atomically within the EVM execution, before the NEAR promise outcome is known.

**Step 2 — The callback args carry `refund: None` when the feature is absent.**

In `native.rs`, the `ExitToNearPrecompileCallbackArgs` struct is populated as:

```rust
let callback_args = ExitToNearPrecompileCallbackArgs {
    #[cfg(feature = "error_refund")]
    refund: refund_call_args(&exit_to_near_params, &exit_event),
    #[cfg(not(feature = "error_refund"))]
    refund: None,          // ← always None when feature is absent
    transfer_near: transfer_near_args,
};
``` [1](#0-0) 

**Step 3 — The callback silently discards the failure.**

In `exit_to_near_precompile_callback`:

```rust
let maybe_result = if let Some(PromiseResult::Successful(_)) = handler.promise_result(0) {
    // success path
    None
} else if let Some(args) = args.refund {
    // refund path — unreachable when error_refund is absent
    let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;
    ...
} else {
    None   // ← promise failed, refund is None → tokens permanently lost
};
``` [2](#0-1) 

When `error_refund` is not compiled in, `args.refund` is always `None`, so a failed `ft_transfer` always falls into the final `else { None }` branch. No re-mint of ERC-20 tokens occurs, and no ETH is transferred back from the precompile address.

**Step 4 — The refund logic itself confirms the two-source gap.**

`refund_on_error` handles both cases correctly when called — ERC-20 re-mint and ETH transfer back from the precompile — but it is **never called** when the feature is absent: [3](#0-2) 

**Step 5 — Test code explicitly documents the permanent loss.**

The test suite acknowledges this behavior:

```rust
#[cfg(feature = "error_refund")]
let balance = FT_TRANSFER_AMOUNT.into();
// If the refund feature is not enabled then there is no refund in the EVM
#[cfg(not(feature = "error_refund"))]
let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();
``` [4](#0-3) 

This confirms that without the feature, the exit amount is simply gone.

---

### Impact Explanation

- **ERC-20 exit path:** User's ERC-20 tokens are burned in the EVM. If `ft_transfer` on the NEP-141 contract fails (e.g., recipient not registered, NEP-141 paused, insufficient Aurora NEP-141 balance), the tokens are permanently destroyed. The NEP-141 balance held by Aurora is not reduced, creating an accounting discrepancy (Aurora holds NEP-141 tokens that no ERC-20 counterpart exists for), and the user loses their funds.
- **ETH exit path:** ETH is transferred to `exit_to_near::ADDRESS`. If the `ft_transfer` fails, the ETH remains locked in that precompile address with no mechanism to recover it.

Both outcomes are **permanent freezing of funds** / **direct theft of user funds** (Critical). [5](#0-4) 

---

### Likelihood Explanation

Any unprivileged EVM user who calls `exitToNear` (directly or via an ERC-20 contract's burn function) is exposed. The `ft_transfer` promise can fail for multiple realistic reasons:

- The NEAR recipient account is not registered with the NEP-141 token.
- The NEP-141 contract is paused or has insufficient balance on Aurora's side.
- The NEP-141 contract panics for any reason.

The entry path is fully user-controlled: submit an EVM transaction calling the `exitToNear` precompile address with a recipient that will cause `ft_transfer` to fail. [6](#0-5) 

---

### Recommendation

The `error_refund` feature should be unconditionally enabled in production builds, or the refund path should be made unconditional in code (not gated behind a compile-time feature). The `ExitToNearPrecompileCallbackArgs::refund` field should always be populated with the correct `RefundCallArgs` so that `exit_to_near_precompile_callback` can always recover user funds on promise failure.

Concretely, remove the `#[cfg(not(feature = "error_refund"))] refund: None` branch and always compute `refund_call_args(...)`:

```rust
let callback_args = ExitToNearPrecompileCallbackArgs {
    refund: refund_call_args(&exit_to_near_params, &exit_event),
    transfer_near: transfer_near_args,
};
``` [1](#0-0) 

---

### Proof of Concept

1. Deploy Aurora without the `error_refund` feature.
2. Bridge a NEP-141 token to an ERC-20 on Aurora.
3. Call the ERC-20's exit function targeting an unregistered NEAR account (so `ft_transfer` will fail).
4. Observe: ERC-20 tokens are burned, `exit_to_near_precompile_callback` is called, `args.refund` is `None`, the `else { None }` branch executes, no re-mint occurs.
5. User's ERC-20 balance is zero; NEP-141 balance on Aurora is unchanged; tokens are permanently lost.

The test `test_exit_to_near_refund` in `engine-tests/src/tests/erc20_connector.rs` already demonstrates this exact scenario and explicitly asserts the reduced balance when `error_refund` is absent. [7](#0-6)

### Citations

**File:** engine-precompiles/src/native.rs (L381-417)
```rust
impl<I: IO> Precompile for ExitToNear<I> {
    fn required_gas(_input: &[u8]) -> Result<EthGas, ExitError> {
        Ok(costs::EXIT_TO_NEAR_GAS)
    }

    #[allow(clippy::too_many_lines)]
    fn run(
        &self,
        input: &[u8],
        target_gas: Option<EthGas>,
        context: &Context,
        is_static: bool,
    ) -> EvmPrecompileResult {
        // ETH (base) transfer input format: (85 bytes)
        //  - flag (1 byte)
        //  - refund_address (20 bytes), present if the feature "error_refund" is enabled
        //  - recipient_account_id (max MAX_INPUT_SIZE - 20 - 1 bytes)
        // ERC-20 transfer input format: (124 bytes)
        //  - flag (1 byte)
        //  - refund_address (20 bytes), present if the feature "error_refund" is enabled.
        //  - amount (32 bytes)
        //  - recipient_account_id (max MAX_INPUT_SIZE - 1 - (20) - 32 bytes)
        //  - `:unwrap` suffix in a case of wNEAR (7 bytes)
        let required_gas = Self::required_gas(input)?;

        if let Some(target_gas) = target_gas
            && required_gas > target_gas
        {
            return Err(ExitError::OutOfGas);
        }

        // It's not allowed to call exit precompiles in static mode
        if is_static {
            return Err(ExitError::Other(Cow::from("ERR_INVALID_IN_STATIC")));
        } else if context.address != exit_to_near::ADDRESS.raw() {
            return Err(ExitError::Other(Cow::from("ERR_INVALID_IN_DELEGATE")));
        }
```

**File:** engine-precompiles/src/native.rs (L449-455)
```rust
        let callback_args = ExitToNearPrecompileCallbackArgs {
            #[cfg(feature = "error_refund")]
            refund: refund_call_args(&exit_to_near_params, &exit_event),
            #[cfg(not(feature = "error_refund"))]
            refund: None,
            transfer_near: transfer_near_args,
        };
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

**File:** engine-tests/src/tests/erc20_connector.rs (L623-666)
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
    }
```
