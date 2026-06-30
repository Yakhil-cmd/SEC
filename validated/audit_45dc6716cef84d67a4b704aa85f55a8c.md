### Title
Permanent ERC-20/ETH Fund Loss When `ExitToNear` Promise Fails Without `error_refund` Feature - (`engine-precompiles/src/native.rs`)

---

### Summary

When the `error_refund` Cargo feature is not compiled in (the production default), a failed `ft_transfer` or `near_withdraw` NEAR promise during an `ExitToNear` precompile call results in the user's ERC-20 tokens being permanently burned (or ETH permanently deducted) with no corresponding NEP-141 receipt and no on-chain refund path. The accounting invariant `sum(tokens_burned_on_EVM) = sum(tokens_received_on_NEAR)` is broken, directly analogous to the M-7 invariant `sum(funding_paid) = sum(funding_received)`.

---

### Finding Description

The `ExitToNear` precompile in `engine-precompiles/src/native.rs` handles ERC-20 and ETH withdrawals from Aurora EVM to NEAR. The flow is:

1. ERC-20 tokens are **burned** by `EvmErc20.withdrawToNear` (or ETH is deducted from the sender's EVM balance).
2. The precompile schedules a NEAR promise (`ft_transfer` or `near_withdraw`) to deliver the corresponding NEP-141 tokens.
3. Optionally, a callback `exit_to_near_precompile_callback` is attached to handle failures and re-mint/refund.

The critical conditional compilation block in `ExitToNear::run`:

```rust
let callback_args = ExitToNearPrecompileCallbackArgs {
    #[cfg(feature = "error_refund")]
    refund: refund_call_args(&exit_to_near_params, &exit_event),
    #[cfg(not(feature = "error_refund"))]
    refund: None,   // <-- hardcoded None in production
    transfer_near: transfer_near_args,
};
``` [1](#0-0) 

When `error_refund` is absent, `refund` is always `None`. For standard ERC-20 exits (non-wNEAR), `transfer_near` is also `None`, so `callback_args == ExitToNearPrecompileCallbackArgs::default()` and **no callback is attached at all**:

```rust
let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
    PromiseArgs::Create(transfer_promise)   // bare promise, no failure handler
} else {
    PromiseArgs::Callback(...)
};
``` [2](#0-1) 

For wNEAR exits, a callback is attached (because `transfer_near` is `Some`), but the failure branch in `exit_to_near_precompile_callback` is a no-op without `error_refund`:

```rust
} else if let Some(args) = args.refund {
    // refund path — never reached without error_refund
    let refund_result = engine::refund_on_error(...)?;
    ...
} else {
    None   // tokens are gone, nothing happens
};
``` [3](#0-2) 

The test suite explicitly acknowledges this behavior:

```rust
// If the refund feature is not enabled then there is no refund in the EVM
#[cfg(not(feature = "error_refund"))]
let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();
``` [4](#0-3) 

---

### Impact Explanation

When the NEAR-side `ft_transfer` promise fails (e.g., recipient account not registered with the NEP-141 contract, insufficient gas, or any other NEAR-level failure), the result is:

- **ERC-20 case**: Tokens are burned on the EVM side via `_burn` in `EvmErc20.withdrawToNear`, but the NEP-141 `ft_transfer` never completes. The ERC-20 supply is permanently reduced with no corresponding NEP-141 delivery. No re-mint occurs. Funds are permanently frozen.
- **ETH case**: ETH is deducted from the sender's EVM balance and transferred to the `ExitToNear` precompile address, but the NEP-141 `ft_transfer` never completes. The ETH sits at the precompile address with no recovery path.

This is a **permanent freezing of user funds** (Critical severity). The accounting invariant `sum(ERC-20 burned) = sum(NEP-141 received)` is broken, directly mirroring M-7's `sum(funding_paid) = sum(funding_received)` invariant violation. [5](#0-4) 

---

### Likelihood Explanation

The trigger condition — a failed `ft_transfer` NEAR promise — is realistic and user-reachable without any privileged access:

- A user exits to a NEAR account that is not registered with the NEP-141 contract (a common mistake).
- The NEP-141 contract is paused or has insufficient storage.
- The attached gas (`FT_TRANSFER_GAS = 10 TGas`) is insufficient for the target contract.

Any of these conditions, which can occur in normal usage, will silently burn the user's tokens with no recovery. [6](#0-5) 

---

### Recommendation

Enable the `error_refund` feature in the production build, or unconditionally populate the `refund` field in `ExitToNearPrecompileCallbackArgs` so that a failure callback always re-mints burned ERC-20 tokens (or returns ETH from the precompile address to the sender). The `refund_on_error` function already implements the correct re-mint logic for both ERC-20 and ETH cases. [7](#0-6) 

---

### Proof of Concept

1. Deploy an ERC-20 bridged token on Aurora (NEP-141 backed).
2. Call `withdrawToNear(recipient_bytes, amount)` on the ERC-20 contract, where `recipient_bytes` encodes a NEAR account **not registered** with the NEP-141 contract.
3. The ERC-20 `_burn` executes, reducing the user's EVM balance.
4. The `ExitToNear` precompile schedules a bare `ft_transfer` promise (no callback, because `error_refund` is not compiled in and `transfer_near` is `None`).
5. The `ft_transfer` NEAR promise fails (unregistered recipient).
6. No callback fires. No re-mint occurs.
7. Observe: user's ERC-20 balance is reduced, NEP-141 balance of recipient is unchanged, and the NEP-141 balance held by the Aurora contract is unchanged — the tokens are permanently lost.

The test `test_exit_to_near_refund` in `engine-tests/src/tests/erc20_connector.rs` already demonstrates this exact scenario and confirms the balance discrepancy when `error_refund` is absent. [8](#0-7)

### Citations

**File:** engine-precompiles/src/native.rs (L53-54)
```rust
    pub(super) const FT_TRANSFER_GAS: NearGas = NearGas::new(10_000_000_000_000);

```

**File:** engine-precompiles/src/native.rs (L436-447)
```rust
                // This precompile branch is expected to be called from the ERC-20 burn function.
                //
                // Input slice format:
                //  amount (U256 big-endian bytes) - the amount that was burned
                //  recipient_account_id (bytes) - the NEAR recipient account which will receive
                //  NEP-141 tokens, or also can contain the `:unwrap` suffix in case of withdrawing
                //  wNEAR, or another message of JSON in case of OMNI, or address of receiver in case
                //  of transfer tokens to another engine contract.
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
