### Title
No Refund Callback on Failed `ExitToEthereum` Withdrawal Promise Causes Permanent Fund Freeze - (File: `engine-precompiles/src/native.rs`)

---

### Summary

The `ExitToEthereum` precompile schedules a bare `PromiseArgs::Create` promise (no callback) to call `withdraw` on the ETH connector. If that promise fails for any reason, the user's ETH or ERC-20 tokens — already deducted from EVM state — are permanently lost with no refund path.

---

### Finding Description

When an EVM user calls the `ExitToEthereum` precompile (address `0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab`) to bridge ETH or ERC-20 tokens back to Ethereum, the precompile's `run` function deducts the caller's EVM balance (for ETH) or burns the ERC-20 tokens (via the calling ERC-20 contract) as part of the EVM execution. It then schedules a NEAR cross-contract call to the ETH connector's `withdraw` method.

The critical issue is at lines 977–985 of `engine-precompiles/src/native.rs`:

```rust
let withdraw_promise = PromiseCreateArgs {
    target_account_id: nep141_address,
    method: "withdraw".to_string(),
    args: serialized_args,
    attached_balance: Yocto::new(1),
    attached_gas: costs::WITHDRAWAL_GAS,
};

let promise = borsh::to_vec(&PromiseArgs::Create(withdraw_promise)).unwrap();
```

This is always a bare `PromiseArgs::Create` — **there is no callback**. If the `withdraw` call on the ETH connector fails (e.g., the connector is paused, the connector account is in an unexpected state, or gas is insufficient), the promise failure is silently ignored. The EVM-side deduction has already been committed, and there is no `exit_to_ethereum_precompile_callback` equivalent to refund the user.

Contrast this with `ExitToNear`, which conditionally attaches a `PromiseArgs::Callback` pointing to `exit_to_near_precompile_callback` when the `error_refund` feature is enabled:

```rust
let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
    PromiseArgs::Create(transfer_promise)
} else {
    PromiseArgs::Callback(PromiseWithCallbackArgs { ... })
};
```

`ExitToEthereum` has no such conditional callback path at all — not even behind a feature flag.

---

### Impact Explanation

**Critical — Permanent freezing of funds.**

When the `withdraw` NEAR promise fails:
- For ETH exits: the ETH was already transferred from the caller's EVM balance to the `ExitToEthereum` precompile address during EVM execution. The NEP-141 `withdraw` on the connector never completes, so the ETH is neither returned to the user nor delivered to Ethereum. It is permanently locked.
- For ERC-20 exits: the ERC-20 tokens were already burned by the calling ERC-20 contract before the precompile ran. The NEP-141 `withdraw` failing means the tokens are burned with no Ethereum-side delivery and no re-mint refund.

In both cases, the user's funds are permanently frozen with no recovery path.

---

### Likelihood Explanation

**Medium.** The `withdraw` promise can fail under realistic conditions:

1. **ETH connector paused**: The engine supports pausing FT transfers for the internal ETH connector (added in v3.6.3). If the connector is paused between the EVM transaction submission and the async promise execution, the `withdraw` call fails.
2. **Connector account state**: Any transient failure in the ETH connector contract (e.g., storage exhaustion, contract upgrade in progress) causes the promise to fail.
3. **Insufficient attached gas**: If `costs::WITHDRAWAL_GAS` is misconfigured or the connector's `withdraw` method requires more gas than allocated, the promise fails.

Any EVM user performing a bridge-out to Ethereum is exposed to this risk on every transaction.

---

### Recommendation

Add a callback to the `ExitToEthereum` promise, analogous to `exit_to_near_precompile_callback`, that checks the promise result and refunds the user's EVM balance (for ETH) or re-mints the ERC-20 tokens (for ERC-20) if the `withdraw` call failed. The pattern already exists in `ExitToNear` and `refund_on_error`:

```rust
// Instead of:
let promise = borsh::to_vec(&PromiseArgs::Create(withdraw_promise)).unwrap();

// Use:
let promise = borsh::to_vec(&PromiseArgs::Callback(PromiseWithCallbackArgs {
    base: withdraw_promise,
    callback: PromiseCreateArgs {
        target_account_id: self.current_account_id.clone(),
        method: "exit_to_ethereum_precompile_callback".to_string(),
        args: borsh::to_vec(&refund_args).unwrap(),
        attached_balance: Yocto::new(0),
        attached_gas: costs::EXIT_TO_ETHEREUM_CALLBACK_GAS,
    },
})).unwrap();
```

The callback should call `engine::refund_on_error` on failure, mirroring the `exit_to_near_precompile_callback` implementation.

---

### Proof of Concept

**Root cause — no callback in `ExitToEthereum::run`:** [1](#0-0) 

**Contrast: `ExitToNear` has a conditional callback with refund:** [2](#0-1) 

**The refund callback handler that `ExitToEthereum` lacks:** [3](#0-2) 

**`refund_on_error` re-mints ERC-20 or transfers ETH back:** [4](#0-3) 

**Test confirming `ExitToNear` has refund but `ExitToEthereum` has no equivalent:** [5](#0-4) 

The comment at line 658–660 explicitly acknowledges: *"If the refund feature is not enabled then there is no refund in the EVM"* — but for `ExitToEthereum`, there is **never** a refund regardless of any feature flag, making this a permanent and unconditional fund-freeze risk for all Ethereum-bound exits.

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

**File:** engine-precompiles/src/native.rs (L977-985)
```rust
        let withdraw_promise = PromiseCreateArgs {
            target_account_id: nep141_address,
            method: "withdraw".to_string(),
            args: serialized_args,
            attached_balance: Yocto::new(1),
            attached_gas: costs::WITHDRAWAL_GAS,
        };

        let promise = borsh::to_vec(&PromiseArgs::Create(withdraw_promise)).unwrap();
```

**File:** engine/src/contract_methods/connector.rs (L196-246)
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
