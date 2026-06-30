### Title
Missing Failure-Check Callback on `ft_transfer` Promise in `ExitToNear` Precompile Causes Permanent ERC-20 Token Loss — (`engine-precompiles/src/native.rs`)

---

### Summary

When the `error_refund` compile-time feature is disabled, the `ExitToNear` precompile burns ERC-20 tokens synchronously inside the EVM and then schedules a fire-and-forget NEAR promise to call `ft_transfer` on the corresponding NEP-141 contract. No callback is attached to detect whether the `ft_transfer` succeeded. If the promise fails (e.g., the recipient NEAR account is not registered with the NEP-141 contract), the burned ERC-20 tokens are permanently lost with no refund path.

---

### Finding Description

The `ExitToNear` precompile in `engine-precompiles/src/native.rs` handles ERC-20-to-NEAR bridge exits. Its `run()` method constructs a `callback_args` struct and decides whether to attach a callback promise based on whether that struct equals its default value: [1](#0-0) 

```rust
let callback_args = ExitToNearPrecompileCallbackArgs {
    #[cfg(feature = "error_refund")]
    refund: refund_call_args(&exit_to_near_params, &exit_event),
    #[cfg(not(feature = "error_refund"))]
    refund: None,
    transfer_near: transfer_near_args,
};
...
let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
    PromiseArgs::Create(transfer_promise)          // fire-and-forget
} else {
    PromiseArgs::Callback(PromiseWithCallbackArgs { // callback attached
        base: transfer_promise,
        callback: PromiseCreateArgs {
            method: "exit_to_near_precompile_callback".to_string(),
            ...
        },
    })
};
```

For the **legacy ERC-20 exit path** (the most common case), `transfer_near_args` is `None`: [2](#0-1) 

When `error_refund` is **not** compiled in, `refund` is hardcoded to `None`: [3](#0-2) 

This means `callback_args == ExitToNearPrecompileCallbackArgs::default()` evaluates to `true`, and the promise degrades to a bare `PromiseArgs::Create` — a fire-and-forget call to `ft_transfer` on the NEP-141 contract with no success verification.

The `exit_to_near_precompile_callback` handler that would detect failure and trigger a refund: [4](#0-3) 

…is **never scheduled** in this code path. The ERC-20 burn has already been committed to EVM state before the promise is even created, so there is no rollback.

The `error_refund` feature also changes the minimum input size, meaning the ERC-20 contracts must be compiled differently to supply a `refund_address`: [5](#0-4) 

This coupling makes it non-trivial to enable the feature retroactively without breaking existing ERC-20 contract calldata.

---

### Impact Explanation

**Critical — Permanent freezing of funds.**

When `ft_transfer` fails (e.g., the destination NEAR account is not registered with the NEP-141 contract), the ERC-20 tokens are already burned inside the EVM. There is no callback, no refund, and no recovery path. The tokens are permanently destroyed on the EVM side while never arriving on the NEAR side. This is confirmed by the engine's own test suite: [6](#0-5) 

```rust
// If the refund feature is not enabled then there is no refund in the EVM
#[cfg(not(feature = "error_refund"))]
let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();
```

The test explicitly acknowledges that `FT_EXIT_AMOUNT` tokens are permanently lost when `error_refund` is disabled.

---

### Likelihood Explanation

**Medium.** Any unprivileged EVM user who calls `exit_to_near` with a recipient NEAR account that is not registered with the NEP-141 contract will trigger this loss. NEAR's NEP-141 standard requires explicit `storage_deposit` registration before an account can receive tokens. A user who specifies a fresh or unregistered NEAR account ID as the exit destination — a common mistake — will silently lose their tokens. No special privileges or attacker cooperation are required; the user is the victim of their own valid transaction.

---

### Recommendation

1. **Always attach the `exit_to_near_precompile_callback`** regardless of the `error_refund` feature flag. The callback should check `PromiseResult::Successful` and, on failure, re-mint the burned ERC-20 tokens to the original sender.
2. If the `error_refund` feature flag is intentionally kept, ensure it is **enabled in all production builds** and that the corresponding ERC-20 contracts supply the `refund_address` field in their calldata.
3. Consider pre-validating the recipient account's registration status before burning tokens, or using `ft_transfer_call` with a fallback that returns tokens to Aurora on failure.

---

### Proof of Concept

1. Deploy a NEP-141 token and bridge it to Aurora as an ERC-20.
2. As a user, hold some ERC-20 balance.
3. Call the ERC-20's `withdraw` function specifying a NEAR account that has **not** called `storage_deposit` on the NEP-141 contract.
4. The ERC-20 contract burns the tokens and calls the `ExitToNear` precompile.
5. The precompile (compiled without `error_refund`) creates `PromiseArgs::Create(ft_transfer_promise)` — no callback.
6. The `ft_transfer` promise fails on the NEAR runtime because the recipient is unregistered.
7. No callback fires; no refund is issued.
8. The user's ERC-20 balance is zero; their NEP-141 balance is zero. Funds are permanently frozen. [7](#0-6) [8](#0-7) [9](#0-8)

### Citations

**File:** engine-precompiles/src/native.rs (L36-39)
```rust
#[cfg(not(feature = "error_refund"))]
const MIN_INPUT_SIZE: usize = 3;
#[cfg(feature = "error_refund")]
const MIN_INPUT_SIZE: usize = 21;
```

**File:** engine-precompiles/src/native.rs (L449-488)
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
```

**File:** engine-precompiles/src/native.rs (L627-646)
```rust
        _ => {
            // There is no way to inject json, given the encoding of both arguments
            // as decimal and valid account id respectively.
            (
                nep141_account_id,
                format!(
                    r#"{{"receiver_id":"{}","amount":"{}"}}"#,
                    exit_params.receiver_account_id,
                    exit_params.amount.as_u128()
                ),
                "ft_transfer",
                None,
                events::ExitToNear::Legacy(ExitToNearLegacy {
                    sender: Address::new(erc20_address),
                    erc20_address: Address::new(erc20_address),
                    dest: exit_params.receiver_account_id.to_string(),
                    amount: exit_params.amount,
                }),
            )
        }
```

**File:** engine/src/contract_methods/connector.rs (L196-244)
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
