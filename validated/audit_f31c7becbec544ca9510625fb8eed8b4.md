### Title
Permanent Fund Freeze When `ExitToNear` Promise Fails Without `error_refund` Feature — (File: `engine-precompiles/src/native.rs`)

### Summary
The `ExitToNear` precompile burns ERC-20 tokens (or deducts ETH from the EVM balance) before scheduling a NEAR `ft_transfer` promise. When the `error_refund` compile-time feature is absent, the `refund` field in the callback args is hardcoded to `None`, and no callback is attached to the outbound promise. If the `ft_transfer` promise fails, the burned tokens are permanently unrecoverable — an exact analog to the "no withdraw function" fund-freeze class.

### Finding Description

In `engine-precompiles/src/native.rs`, the `ExitToNear::run` method constructs callback arguments for the outbound NEAR promise:

```rust
let callback_args = ExitToNearPrecompileCallbackArgs {
    #[cfg(feature = "error_refund")]
    refund: refund_call_args(&exit_to_near_params, &exit_event),
    #[cfg(not(feature = "error_refund"))]
    refund: None,          // ← no refund path compiled in
    transfer_near: transfer_near_args,
};
``` [1](#0-0) 

When `transfer_near_args` is also `None` (the common case for plain `ft_transfer` exits of ERC-20 tokens or ETH), `callback_args` equals the default value, so the promise is emitted with **no callback at all**:

```rust
let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
    PromiseArgs::Create(transfer_promise)   // ← no callback attached
} else {
    PromiseArgs::Callback(PromiseWithCallbackArgs { ... })
};
``` [2](#0-1) 

The `exit_to_near_precompile_callback` handler, which is the only place `refund_on_error` is called, is therefore never scheduled:

```rust
} else if let Some(args) = args.refund {
    let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;
    ...
} else {
    None   // ← silent no-op when refund is None
};
``` [3](#0-2) 

`refund_on_error` itself is the only mechanism that can re-mint burned ERC-20 tokens or transfer ETH back from the `exit_to_near` precompile address to the user: [4](#0-3) 

The integration test explicitly documents the two-path behavior:

```rust
#[cfg(feature = "error_refund")]
let balance = FT_TRANSFER_AMOUNT.into();
// If the refund feature is not enabled then there is no refund in the EVM
#[cfg(not(feature = "error_refund"))]
let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();
``` [5](#0-4) 

### Impact Explanation

**Critical — Permanent freezing of funds.**

When `error_refund` is not compiled into the production WASM:

- ERC-20 tokens are burned inside the EVM (supply reduced, user balance zeroed for the exited amount).
- The corresponding NEP-141 `ft_transfer` promise is dispatched to the external token contract.
- If that promise fails for any reason (recipient account not registered with the NEP-141 contract, Aurora's NEP-141 balance insufficient, contract paused, etc.), no callback fires.
- The burned ERC-20 tokens and the corresponding NEP-141 balance locked in Aurora's account are both irrecoverable — there is no admin withdraw, no retry path, and no on-chain state that records the failed exit for later recovery.

The same applies to ETH base-token exits: ETH is deducted from the EVM balance before the `ft_transfer` promise is sent; if the promise fails without `error_refund`, the ETH is gone.

### Likelihood Explanation

**Medium.** The trigger condition — a failed `ft_transfer` — is reachable by any unprivileged EVM user:

1. A user calls `ExitToNear` targeting a NEAR account that has never called `storage_deposit` on the NEP-141 contract (unregistered recipient). This is a common user mistake.
2. The `ft_transfer` promise fails because the recipient is not registered.
3. Without `error_refund`, no refund is issued.

The likelihood is bounded by whether the production binary is compiled with `error_refund`. If it is not, every failed exit is a permanent loss. The feature's existence as an opt-in flag (rather than always-on) means deployments without it are exposed.

### Recommendation

1. Make `error_refund` a **default feature** in `engine-precompiles/Cargo.toml` and `engine/Cargo.toml` so it is always compiled into production builds unless explicitly opted out.
2. Alternatively, remove the conditional compilation entirely and always attach the refund callback to every `ExitToNear` promise, accepting the small gas overhead.
3. Add a guard in the precompile that panics at compile time if `error_refund` is absent, making the risk explicit.

### Proof of Concept

**Attacker-controlled entry path:**

1. Deploy any ERC-20 on Aurora backed by a NEP-141 token.
2. Call the `ExitToNear` precompile (`0xe9217bc70b7ed1f598ddd3199e80b093fa71124f`) from an EVM transaction, specifying a NEAR account that is **not registered** with the NEP-141 contract as the recipient.
3. The precompile burns the ERC-20 tokens and schedules `ft_transfer` on the NEP-141 contract.
4. `ft_transfer` fails (unregistered recipient).
5. Without `error_refund`: no callback fires, `refund_on_error` is never called, tokens are permanently lost.

The test `test_exit_to_near_refund` in `engine-tests/src/tests/erc20_connector.rs` (lines 623–666) demonstrates exactly this scenario and explicitly shows the balance discrepancy when `error_refund` is absent. [6](#0-5)

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
