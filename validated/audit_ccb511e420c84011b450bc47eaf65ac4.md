### Title
Permanent Token Loss When `exitToNear` Promise Fails Without `error_refund` Feature — (`engine-precompiles/src/native.rs`, `engine/src/contract_methods/connector.rs`)

### Summary

When the `error_refund` compile-time feature is not enabled (the default), the `ExitToNear` precompile unconditionally sets `refund: None` in its callback arguments. If the downstream NEAR `ft_transfer` promise fails, the callback `exit_to_near_precompile_callback` silently does nothing — no refund is issued. Because the ERC-20 tokens were already burned before the promise was scheduled, they are permanently lost.

### Finding Description

The `ExitToNear` precompile in `engine-precompiles/src/native.rs` constructs callback arguments that conditionally include refund data:

```rust
let callback_args = ExitToNearPrecompileCallbackArgs {
    #[cfg(feature = "error_refund")]
    refund: refund_call_args(&exit_to_near_params, &exit_event),
    #[cfg(not(feature = "error_refund"))]
    refund: None,          // <-- always None in default build
    transfer_near: transfer_near_args,
};
``` [1](#0-0) 

The `error_refund` feature is **not** listed under `default` in `engine/Cargo.toml`, meaning the production WASM binary is compiled without it:

```toml
[features]
default = ["std"]
...
error_refund = ["aurora-engine-precompiles/error_refund"]
``` [2](#0-1) 

In the callback handler `exit_to_near_precompile_callback`, when the NEAR promise fails and `args.refund` is `None`, execution falls into the silent `else { None }` branch — no refund is performed:

```rust
let maybe_result = if let Some(PromiseResult::Successful(_)) = handler.promise_result(0) {
    // success path
    ...
} else if let Some(args) = args.refund {
    // refund path — only reachable if error_refund feature is enabled
    let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;
    ...
} else {
    None  // <-- silent no-op: tokens already burned, no refund
};
``` [3](#0-2) 

The codebase's own test suite explicitly acknowledges this behavior:

```rust
#[cfg(feature = "error_refund")]
let balance = FT_TRANSFER_AMOUNT.into();
// If the refund feature is not enabled then there is no refund in the EVM
#[cfg(not(feature = "error_refund"))]
let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();
``` [4](#0-3) 

The same permanent-loss behavior applies to ETH (base token) exits, confirmed by `test_exit_to_near_eth_refund`: [5](#0-4) 

### Impact Explanation

**Critical — Permanent freezing/loss of funds.**

When a user calls `exitToNear` (ERC-20 or ETH base token), the tokens are burned from the EVM state before the NEAR `ft_transfer` promise is dispatched. If that promise fails for any reason (e.g., recipient account not registered with the NEP-141 token, insufficient storage deposit, or any other NEAR-side rejection), the callback receives `refund: None` and silently returns. The burned tokens are never re-minted. They are permanently destroyed with no recovery path.

### Likelihood Explanation

**High.** The trigger condition — a NEAR `ft_transfer` failing because the recipient is not registered with the NEP-141 token — is a routine, user-reachable scenario. Any unprivileged EVM user who calls `exitToNear` with a NEAR account that has not called `storage_deposit` on the target NEP-141 contract will trigger this. No special access or admin compromise is required. The `error_refund` feature being off-by-default means every standard production deployment is affected.

### Recommendation

1. Enable `error_refund` in the `default` feature set of `engine/Cargo.toml` so that refund data is always populated in the callback args.
2. Alternatively, restructure the callback so that a failed promise with `refund: None` still triggers a re-mint of the burned tokens, rather than silently discarding the failure.
3. At minimum, if no refund mechanism is available, the `exitToNear` precompile should not burn tokens until the NEAR promise has been confirmed successful.

### Proof of Concept

1. User holds ERC-20 tokens on Aurora backed by a NEP-141 token.
2. User calls `exitToNear` targeting a NEAR account that has **not** called `storage_deposit` on the NEP-141 contract.
3. The ERC-20 tokens are burned in the EVM state.
4. A NEAR `ft_transfer` promise is scheduled to the NEP-141 contract.
5. The NEP-141 contract rejects the transfer (unregistered recipient).
6. `exit_to_near_precompile_callback` is invoked; `args.refund` is `None` (default build); the `else { None }` branch executes.
7. No re-mint occurs. The user's tokens are permanently gone.

The test `test_exit_to_near_refund` in `engine-tests/src/tests/erc20_connector.rs` (lines 623–665) demonstrates exactly this scenario and confirms the token loss when `error_refund` is not enabled. [6](#0-5)

### Citations

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

**File:** engine/Cargo.toml (L43-48)
```text
default = ["std"]
std = ["aurora-engine-types/std", "aurora-engine-hashchain/std", "aurora-engine-sdk/std", "aurora-engine-precompiles/std", "aurora-engine-transactions/std", "ethabi/std", "aurora-evm/std", "hex/std", "rlp/std", "serde/std", "serde_json/std"]
contract = ["log", "aurora-engine-sdk/contract", "aurora-engine-precompiles/contract"]
log = ["aurora-engine-sdk/log", "aurora-engine-precompiles/log"]
tracing = ["aurora-evm/tracing"]
error_refund = ["aurora-engine-precompiles/error_refund"]
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

**File:** engine-tests/src/tests/erc20_connector.rs (L771-775)
```rust
        #[cfg(feature = "error_refund")]
        let expected_balance = Wei::new_u64(INITIAL_ETH_BALANCE);
        // If the refund feature is not enabled, then there is no refund in the EVM
        #[cfg(not(feature = "error_refund"))]
        let expected_balance = Wei::new_u64(INITIAL_ETH_BALANCE - ETH_EXIT_AMOUNT);
```
