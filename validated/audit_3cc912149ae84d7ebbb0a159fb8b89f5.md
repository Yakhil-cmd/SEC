### Title
Missing Failure Callback in `ExitToEthereum` Precompile Causes Permanent Token Loss on Bridge Withdrawal Failure - (File: `engine-precompiles/src/native.rs`)

---

### Summary

The `ExitToEthereum` precompile burns ERC-20 tokens (or deducts ETH) inside the Aurora EVM and then fires a NEAR promise to call `withdraw` on the ETH connector contract. Unlike the `ExitToNear` precompile — which conditionally attaches a `exit_to_near_precompile_callback` to refund tokens if the outbound transfer fails — `ExitToEthereum` creates a bare `PromiseArgs::Create` with no callback and no refund path. If the `withdraw` promise fails on the NEAR side, the tokens are permanently destroyed in the EVM with no recovery mechanism.

---

### Finding Description

The `ExitToEthereum::run()` function in `engine-precompiles/src/native.rs` constructs a single outbound NEAR promise: [1](#0-0) 

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

This is a **fire-and-forget** promise. There is no `PromiseArgs::Callback` wrapping it, and no `exit_to_ethereum_precompile_callback` method exists anywhere in the codebase.

Contrast this with `ExitToNear::run()`, which builds a `PromiseWithCallbackArgs` that attaches `exit_to_near_precompile_callback` to handle the failure case: [2](#0-1) 

The callback in `exit_to_near_precompile_callback` explicitly handles the failure branch by calling `engine::refund_on_error`: [3](#0-2) 

No equivalent exists for `ExitToEthereum`. The two-step sequence is:

1. **Step 1 (EVM, atomic):** ERC-20 tokens are burned by the ERC-20 contract before calling the precompile, or ETH is deducted from the caller's balance via `context.apparent_value`. This step is committed to EVM state.
2. **Step 2 (NEAR promise, non-atomic):** The `withdraw` promise is dispatched to the ETH connector. If this promise fails (ETH connector paused, insufficient gas propagation, connector-side validation error, etc.), NEAR simply discards the failed promise result. No callback fires, no refund is issued.

The result is that the user's tokens are permanently destroyed in the EVM with no corresponding release on Ethereum.

---

### Impact Explanation

**Critical — Permanent freezing/loss of funds.**

When the outbound `withdraw` NEAR promise fails:
- ERC-20 tokens are already burned in the Aurora EVM (irreversible within the EVM state).
- No ETH or ERC-20 equivalent is released on Ethereum.
- No refund is minted back to the user in Aurora.
- The tokens are permanently lost with no recovery path.

This matches the analog vulnerability class exactly: a multi-step operation where the first step (burn) commits irreversibly, and the second step (NEAR-side withdrawal) can fail silently with no rollback.

---

### Likelihood Explanation

The `withdraw` NEAR promise can fail under realistic conditions:

- The ETH connector contract is paused or in a maintenance state (a documented operational mode for bridge contracts).
- The ETH connector rejects the withdrawal due to an internal invariant check or accounting mismatch.
- Gas exhaustion in the promise chain if the `WITHDRAWAL_GAS` constant (`100 TGas`) is insufficient for a particular ETH connector version. [4](#0-3) 

Any EVM user who calls an ERC-20 token's `withdraw` or `burn` function that internally invokes the `exitToEthereum` precompile at address `0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab` is exposed to this risk: [5](#0-4) 

The entry path requires no special privileges — any token holder can trigger it.

---

### Recommendation

Implement a failure callback for `ExitToEthereum` analogous to the `exit_to_near_precompile_callback` pattern already used in `ExitToNear`:

1. Wrap the `withdraw_promise` in a `PromiseWithCallbackArgs` that targets a new `exit_to_ethereum_precompile_callback` method on the engine.
2. In that callback, check `handler.promise_result(0)`. If the result is not `PromiseResult::Successful`, call `engine::refund_on_error` to re-mint the burned ERC-20 tokens (or re-credit ETH) to the original sender's EVM address.
3. Pass the necessary refund parameters (sender address, ERC-20 address, amount) through the callback args, mirroring the `ExitToNearPrecompileCallbackArgs` / `RefundCallArgs` pattern already defined in the types crate. [6](#0-5) 

---

### Proof of Concept

1. User holds 100 units of a bridged ERC-20 token on Aurora.
2. User calls the ERC-20 contract's `withdraw(100, eth_recipient)` function.
3. The ERC-20 contract burns 100 tokens and calls the `exitToEthereum` precompile (`0xb0bd02f6...`).
4. The precompile constructs `PromiseArgs::Create(withdraw_promise)` targeting the ETH connector's `withdraw` method and returns it as a log.
5. The Aurora Engine dispatches the NEAR promise.
6. The ETH connector's `withdraw` call fails (e.g., connector is paused).
7. NEAR discards the failed promise. No callback fires.
8. The user's 100 ERC-20 tokens are permanently burned. No ETH arrives on Ethereum. No refund is issued in Aurora.

The root cause is at: [1](#0-0) 

compared to the existing (but absent for `ExitToEthereum`) refund pattern at: [2](#0-1)

### Citations

**File:** engine-precompiles/src/native.rs (L61-62)
```rust
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

**File:** engine-precompiles/src/native.rs (L821-829)
```rust
pub mod exit_to_ethereum {
    use crate::prelude::types::{Address, make_address};

    /// Exit to Ethereum precompile address
    ///
    /// Address: `0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab`
    /// This address is computed as: `&keccak("exitToEthereum")[12..]`
    pub const ADDRESS: Address = make_address(0xb0bd02f6, 0xa392af548bdf1cfaee5dfa0eefcc8eab);
}
```

**File:** engine-precompiles/src/native.rs (L977-990)
```rust
        let withdraw_promise = PromiseCreateArgs {
            target_account_id: nep141_address,
            method: "withdraw".to_string(),
            args: serialized_args,
            attached_balance: Yocto::new(1),
            attached_gas: costs::WITHDRAWAL_GAS,
        };

        let promise = borsh::to_vec(&PromiseArgs::Create(withdraw_promise)).unwrap();
        let promise_log = Log {
            address: exit_to_ethereum::ADDRESS.raw(),
            topics: Vec::new(),
            data: promise,
        };
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
