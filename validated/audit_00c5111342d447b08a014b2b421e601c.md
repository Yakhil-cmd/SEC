### Title
`ExitToNear` Precompile Permanently Freezes Tokens When NEAR Transfer Fails Without `error_refund` Feature - (`engine-precompiles/src/native.rs`)

### Summary

When the `error_refund` compile-time feature is not enabled, the `ExitToNear` precompile schedules a NEAR `ft_transfer` (or `ft_transfer_call`) promise with **no callback**. If that promise fails (e.g., the recipient NEAR account is not registered with the NEP-141 contract), the ERC-20 tokens are permanently burned on the EVM side with no recovery path — an exact structural analog to the `withdrawFor` fund-lock described in the report.

### Finding Description

The `ExitToNear::run()` function constructs `callback_args` conditionally on the `error_refund` feature:

```rust
let callback_args = ExitToNearPrecompileCallbackArgs {
    #[cfg(feature = "error_refund")]
    refund: refund_call_args(&exit_to_near_params, &exit_event),
    #[cfg(not(feature = "error_refund"))]
    refund: None,
    transfer_near: transfer_near_args,
};
``` [1](#0-0) 

For all standard ERC-20 and ETH exits (where `transfer_near` is `None`), when `error_refund` is not enabled, `callback_args` equals the default value. The code then takes the no-callback branch:

```rust
let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
    PromiseArgs::Create(transfer_promise)   // <-- no callback attached
} else {
    PromiseArgs::Callback(PromiseWithCallbackArgs { ... })
};
``` [2](#0-1) 

The ERC-20 burn (or ETH deduction) happens atomically inside the EVM execution before the promise is scheduled. If the subsequent NEAR `ft_transfer` promise fails, there is no `exit_to_near_precompile_callback` to invoke `refund_on_error`, so the burned tokens are irrecoverable.

The `exit_to_near_precompile_callback` that would otherwise handle the refund is:

```rust
} else if let Some(args) = args.refund {
    let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;
    ...
}
``` [3](#0-2) 

This branch is never reached because no callback is scheduled.

The test suite explicitly acknowledges this behavior:

```rust
// If the refund feature is not enabled then there is no refund in the EVM
#[cfg(not(feature = "error_refund"))]
let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();
``` [4](#0-3) 

### Impact Explanation

**Permanent freezing of funds.** When a user calls the `ExitToNear` precompile to bridge ERC-20 tokens to a NEAR account that is not registered with the NEP-141 contract:

1. ERC-20 tokens are burned from the user's EVM balance.
2. The `ft_transfer` NEAR promise fails (unregistered recipient).
3. No callback fires; `refund_on_error` is never called.
4. The burned tokens are permanently lost — they no longer exist on the EVM side and were never credited on the NEAR side.

There is no admin function, migration path, or user-callable recovery mechanism to retrieve these tokens.

### Likelihood Explanation

Any unprivileged EVM user can trigger this by calling the `ExitToNear` precompile (directly or via an ERC-20 `withdraw` function) with a NEAR `receiver_account_id` that has not performed `storage_deposit` on the target NEP-141 contract. This is a realistic mistake — NEAR's storage-staking model requires explicit registration before receiving NEP-141 tokens, and users unfamiliar with this requirement will encounter it. The entry path requires no special privileges. [5](#0-4) 

### Recommendation

Ensure the `error_refund` feature is always enabled in production builds, or make the refund callback unconditional. The `refund_call_args` function and `RefundCallArgs` struct already exist for this purpose: [6](#0-5) 

The `refund_address` should default to `context.caller` when not explicitly provided, so that a failed NEAR transfer always re-mints/returns tokens to the originating EVM address without requiring the caller to supply a separate refund address.

### Proof of Concept

1. Deploy an ERC-20 token on Aurora backed by a NEP-141 contract.
2. Call the ERC-20's `withdraw` function (which calls `ExitToNear` precompile) specifying a NEAR `receiver_account_id` that has **not** called `storage_deposit` on the NEP-141 contract.
3. Observe: ERC-20 tokens are burned from the caller's balance.
4. The `ft_transfer` NEAR promise fails with "The account is not registered".
5. Without `error_refund`, no callback fires. The ERC-20 balance is gone and no NEP-141 tokens were credited.
6. The tokens are permanently lost with no recovery path. [7](#0-6)

### Citations

**File:** engine-precompiles/src/native.rs (L444-447)
```rust
                ExitToNearParams::Erc20TokenParams(ref exit_params) => {
                    exit_erc20_token_to_near(context, exit_params, &self.io)?
                }
            };
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

**File:** engine-precompiles/src/native.rs (L470-483)
```rust
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

**File:** engine-precompiles/src/native.rs (L699-725)
```rust
#[cfg(feature = "error_refund")]
#[allow(clippy::unnecessary_wraps)]
fn refund_call_args(
    params: &ExitToNearParams,
    event: &events::ExitToNear,
) -> Option<RefundCallArgs> {
    Some(RefundCallArgs {
        recipient_address: match params {
            ExitToNearParams::BaseToken(params) => params.refund_address,
            ExitToNearParams::Erc20TokenParams(params) => params.refund_address,
        },
        erc20_address: match params {
            ExitToNearParams::BaseToken(_) => None,
            ExitToNearParams::Erc20TokenParams(_) => {
                let erc20_address = match event {
                    events::ExitToNear::Legacy(legacy) => legacy.erc20_address,
                    events::ExitToNear::Omni(omni) => omni.erc20_address,
                };
                Some(erc20_address)
            }
        },
        amount: types::u256_to_arr(&match event {
            events::ExitToNear::Legacy(legacy) => legacy.amount,
            events::ExitToNear::Omni(omni) => omni.amount,
        }),
    })
}
```

**File:** engine/src/contract_methods/connector.rs (L231-239)
```rust
        } else if let Some(args) = args.refund {
            // Exit call failed; need to refund tokens
            let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;

            if !refund_result.status.is_ok() {
                return Err(errors::ERR_REFUND_FAILURE.into());
            }

            Some(refund_result)
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
