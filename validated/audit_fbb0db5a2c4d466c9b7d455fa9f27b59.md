### Title
Insufficient `EXIT_TO_NEAR_CALLBACK_GAS` Causes Permanent ERC-20 Token Loss on Failed `ft_transfer` — (`engine-precompiles/src/native.rs`)

---

### Summary

The `exitToNear` precompile burns ERC-20 tokens in the EVM and schedules a NEP-141 `ft_transfer` promise. A callback (`exit_to_near_precompile_callback`) is attached with a fixed `EXIT_TO_NEAR_CALLBACK_GAS = 10 TGas` to handle refunds if the transfer fails. However, the refund path (`refund_on_error`) runs a full EVM execution with `u64::MAX` EVM gas. If the EVM execution in `refund_on_error` consumes more NEAR gas than the 10 TGas budget, the callback fails silently. Because the EVM burn already committed before the promise chain, the burned ERC-20 tokens are permanently lost with no recovery path.

---

### Finding Description

**Step 1 — EVM burn is committed before the promise chain.**

In `engine-precompiles/src/native.rs`, the `ExitToNear::run()` precompile burns ERC-20 tokens (via the EVM state machine) and then emits a promise log. The burn is part of the EVM transaction that already succeeded by the time the NEAR promise is dispatched. [1](#0-0) 

**Step 2 — Callback gas is a fixed 10 TGas heuristic.**

The callback `exit_to_near_precompile_callback` is attached with `EXIT_TO_NEAR_CALLBACK_GAS = 10 TGas`, described only as "Value determined experimentally based on tests." [2](#0-1) 

**Step 3 — The refund path runs unbounded EVM execution.**

Inside `exit_to_near_precompile_callback`, when the `ft_transfer` promise fails and `error_refund` is enabled, `refund_on_error` is called. This function creates a full `Engine` instance and calls `engine.call()` with `u64::MAX` EVM gas to re-mint the burned tokens. [3](#0-2) 

The NEAR gas cost of this EVM execution is proportional to the EVM gas consumed. For complex ERC-20 contracts (with hooks, callbacks, or large storage), the EVM gas can be arbitrarily high, easily exceeding the 10 TGas callback budget.

**Step 4 — Callback failure leaves tokens permanently burned.**

If `exit_to_near_precompile_callback` runs out of NEAR gas, the callback fails. The EVM burn (Step 1) is already committed and irreversible. The NEP-141 transfer failed. No refund is issued. The user's ERC-20 tokens are permanently destroyed. [4](#0-3) 

**Step 5 — When `error_refund` is disabled, the loss is unconditional.**

When the `error_refund` feature is not compiled in, `args.refund` is always `None`. Any failure of the `ft_transfer` promise — regardless of gas — results in permanent token loss, with no callback gas issue even required. [5](#0-4) 

The test suite explicitly documents this behavior: [6](#0-5) 

---

### Impact Explanation

**Critical — Permanent freezing/theft of user funds.**

A user who calls `exitToNear` on a bridged ERC-20 token has their ERC-20 balance burned in the EVM. If the downstream NEP-141 `ft_transfer` fails (e.g., recipient not registered, NEP-141 contract paused, or insufficient NEAR gas in the callback for complex tokens), and the refund callback also fails or is absent, the user permanently loses their tokens. There is no replay mechanism — the EVM burn is final and the NEAR-side transfer never completes.

---

### Likelihood Explanation

**High.** The `ft_transfer` promise can fail for multiple reasons outside the user's control: the recipient NEAR account is not registered with the NEP-141 contract, the NEP-141 contract is paused, or the NEP-141 contract has been upgraded with more complex logic that consumes more NEAR gas than the 10 TGas callback budget. Any EVM user calling `exitToNear` is exposed to this risk. The `error_refund` feature being a compile-time flag means the production deployment's behavior is determined at build time, not at runtime.

---

### Recommendation

1. **Increase `EXIT_TO_NEAR_CALLBACK_GAS`** to a value that accounts for worst-case EVM execution in `refund_on_error`. The current 10 TGas is a heuristic with no formal upper-bound analysis.
2. **Ensure `error_refund` is always enabled** in production builds, or redesign the flow so that the EVM burn only commits after the NEP-141 transfer is confirmed successful (i.e., use a two-phase commit pattern where the burn is conditional on the promise result).
3. **Add a gas check** before calling `refund_on_error` to ensure sufficient NEAR gas remains in the callback, and if not, emit a recoverable error state rather than silently failing.

---

### Proof of Concept

1. User holds 100 units of a bridged NEP-141 token as ERC-20 on Aurora.
2. User calls `withdrawToNear("unregistered.near", 100)` on the ERC-20 contract.
3. The ERC-20 contract calls the `exitToNear` precompile — 100 tokens are burned in EVM state.
4. Aurora schedules `ft_transfer(receiver_id: "unregistered.near", amount: 100)` on the NEP-141 contract with 10 TGas.
5. The `ft_transfer` fails because `"unregistered.near"` is not registered.
6. The callback `exit_to_near_precompile_callback` is invoked with 10 TGas.
7. If `error_refund` is disabled: `args.refund` is `None`, the `else { None }` branch executes, no refund occurs.
8. If `error_refund` is enabled but the ERC-20 mint in `refund_on_error` consumes more than 10 TGas: the callback panics out-of-gas, no refund occurs.
9. In both cases: the user's 100 ERC-20 tokens are permanently destroyed. [7](#0-6) [8](#0-7) [9](#0-8) [10](#0-9)

### Citations

**File:** engine-precompiles/src/native.rs (L42-62)
```rust
mod costs {
    use crate::prelude::types::{EthGas, NearGas};

    // TODO(#483): Determine the correct amount of gas
    pub(super) const EXIT_TO_NEAR_GAS: EthGas = EthGas::new(0);

    // TODO(#483): Determine the correct amount of gas
    pub(super) const EXIT_TO_ETHEREUM_GAS: EthGas = EthGas::new(0);

    /// Value determined experimentally based on tests and mainnet data. Example:
    /// `https://explorer.mainnet.near.org/transactions/5CD7NrqWpK3H8MAAU4mYEPuuWz9AqR9uJkkZJzw5b8PM#D1b5NVRrAsJKUX2ZGs3poKViu1Rgt4RJZXtTfMgdxH4S`
    pub(super) const FT_TRANSFER_GAS: NearGas = NearGas::new(10_000_000_000_000);

    pub(super) const FT_TRANSFER_CALL_GAS: NearGas = NearGas::new(70_000_000_000_000);

    /// Value determined experimentally based on tests.
    pub(super) const EXIT_TO_NEAR_CALLBACK_GAS: NearGas = NearGas::new(10_000_000_000_000);

    // TODO(#332): Determine the correct amount of gas
    pub(super) const WITHDRAWAL_GAS: NearGas = NearGas::new(100_000_000_000_000);
}
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
