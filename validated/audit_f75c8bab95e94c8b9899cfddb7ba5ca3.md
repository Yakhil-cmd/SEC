### Title
Missing Callback on NEAR Transfer in wNEAR Unwrap Path Causes Permanent Loss of User Funds — (`engine/src/contract_methods/connector.rs`, `engine-precompiles/src/native.rs`)

---

### Summary

When a user calls `EvmErc20.withdrawToNear` on the wNEAR ERC-20 with a `:unwrap` suffix targeting a non-existent NEAR named account, the wNEAR ERC-20 is burned and `near_withdraw` succeeds (NEAR credited to the engine account), but the subsequent `PromiseBatchAction::Transfer` to the non-existent account fails silently. There is no callback on that Transfer promise to re-mint the ERC-20 or return the NEAR to the user. The NEAR is permanently absorbed into the engine account balance with no recovery path.

---

### Finding Description

The wNEAR unwrap flow is a two-step promise chain:

**Step 1 — Precompile (`engine-precompiles/src/native.rs`, `exit_erc20_token_to_near`):**

When the `:unwrap` suffix is detected and the ERC-20 caller matches the registered wNEAR address, the precompile constructs:
- Base promise: `near_withdraw` on the wNEAR NEP-141 contract
- Callback: `exit_to_near_precompile_callback` on the engine [1](#0-0) 

The `transfer_near_args` carries the user-supplied `target_account_id` (e.g. `nonexistent-account.near`) and the amount. [2](#0-1) 

**Step 2 — Callback (`engine/src/contract_methods/connector.rs`, `exit_to_near_precompile_callback`):**

When `near_withdraw` succeeds, the callback creates a `PromiseBatchAction::Transfer` to `args.target_account_id` with **no further callback** attached: [3](#0-2) 

`promise_create_batch` is called and `promise_return` is called on it, but there is no `promise_attach_callback` to handle failure of that Transfer. If the Transfer fails (e.g. because the named account does not exist on NEAR), the NEAR is refunded by the NEAR runtime to the engine account (the predecessor of the batch action), and the function returns `None` — no re-mint, no error propagation.

**The `error_refund` feature does not cover this case.** It only populates `args.refund` to handle the case where `near_withdraw` itself fails (the base promise). The Transfer failure is a third promise in the chain with no error handler at all. [4](#0-3) [5](#0-4) 

The `AccountId` type validates only the syntactic format of the NEAR account ID, not whether the account actually exists on-chain. A user can supply a well-formed but non-existent account ID (e.g. `typo-account.near:unwrap`) and the precompile will accept it without error.

---

### Impact Explanation

The full sequence:

1. wNEAR ERC-20 tokens burned in EVM state (irreversible at this point)
2. `near_withdraw` on wNEAR NEP-141 succeeds → NEAR credited to engine account
3. `exit_to_near_precompile_callback` fires, sees `PromiseResult::Successful`, dispatches `Transfer` to non-existent named account
4. NEAR runtime rejects the Transfer (named account does not exist); NEAR is refunded to the engine account
5. No callback exists to detect this failure and re-mint the ERC-20 or return NEAR to the user
6. User's wNEAR value is permanently lost; NEAR sits in the engine account with no user-accessible recovery path

This matches **Critical — Permanent freezing of funds**. The NEAR is not stolen by an external attacker; it is absorbed into the engine account balance, permanently inaccessible to the user who initiated the withdrawal.

---

### Likelihood Explanation

- Any user who makes a typo in their NEAR recipient account name while using the `:unwrap` suffix will trigger this permanently
- The `AccountId` type provides no on-chain existence check, so the error is silent until the Transfer fails asynchronously
- The existing test suite covers the `near_withdraw` failure refund path (`test_exit_to_near_refund`) but does not cover the Transfer failure path in the wNEAR unwrap flow
- The `error_refund` feature, even when enabled, does not protect against this specific failure mode [6](#0-5) 

---

### Recommendation

In `exit_to_near_precompile_callback`, after dispatching the `PromiseBatchAction::Transfer`, attach a second callback (a new engine method, e.g. `exit_to_near_transfer_callback`) that:
- Checks the Transfer promise result
- On failure, re-mints the wNEAR ERC-20 tokens to the original sender's address (mirroring the existing `refund_on_error` / `engine::refund_on_error` logic used for the `near_withdraw` failure case)

The `ExitToNearPrecompileCallbackArgs` struct should be extended to carry the original sender's EVM address so the re-mint target is available in the second callback. [7](#0-6) [8](#0-7) 

---

### Proof of Concept

Sandbox test outline (unmodified production code):

```rust
// 1. Deploy Aurora, deploy wNEAR NEP-141, bridge wNEAR to ERC-20 on Aurora
// 2. Fund ft_owner with wNEAR ERC-20 tokens
let engine_near_balance_before = aurora.node.get_balance(&aurora.id()).await.unwrap();
let erc20_balance_before = erc20_balance(&wnear_erc20, ft_owner_address, &aurora).await;

// 3. Call withdrawToNear with a non-existent named account + :unwrap suffix
exit_to_near(
    &ft_owner,
    "definitely-does-not-exist-xyz.near:unwrap",  // non-existent named account
    FT_EXIT_AMOUNT,
    &wnear_erc20,
    &aurora,
).await;

aurora.node.skip_blocks(2).await.unwrap(); // allow all receipts to settle

// 4. Assert: ERC-20 balance decreased (tokens burned, not refunded)
assert!(erc20_balance(&wnear_erc20, ft_owner_address, &aurora).await < erc20_balance_before);

// 5. Assert: recipient received nothing
assert_eq!(
    aurora.node.get_balance(&"definitely-does-not-exist-xyz.near".parse().unwrap()).await.unwrap_or(0),
    0
);

// 6. Assert: engine account NEAR balance increased (absorbed the unwrapped NEAR)
assert!(aurora.node.get_balance(&aurora.id()).await.unwrap() > engine_near_balance_before);
```

The test would confirm that the wNEAR ERC-20 is burned, the recipient receives nothing, and the engine account absorbs the NEAR — demonstrating permanent loss of user funds with no refund path.

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

**File:** engine-precompiles/src/native.rs (L587-601)
```rust
        Some(Message::UnwrapWnear) if erc20_address == get_wnear_address(io).raw() =>
        // The flow is following here:
        // 1. We call `near_withdraw` on wNEAR account id on `aurora` behalf.
        // In such way we unwrap wNEAR to NEAR.
        // 2. After that, we call callback `exit_to_near_precompile_callback` on the `aurora`
        // in which make transfer of unwrapped NEAR to the `target_account_id`.
        {
            (
                nep141_account_id,
                format!(r#"{{"amount":"{}"}}"#, exit_params.amount.as_u128()),
                "near_withdraw",
                Some(TransferNearArgs {
                    target_account_id: exit_params.receiver_account_id.clone(),
                    amount: exit_params.amount.as_u128(),
                }),
```

**File:** engine/src/contract_methods/connector.rs (L214-228)
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

**File:** engine-types/src/parameters/connector.rs (L129-134)
```rust
/// Arguments for callback used in the `exit_to_near` precompile.
#[derive(Debug, Clone, BorshSerialize, BorshDeserialize, PartialEq, Eq, Default)]
pub struct ExitToNearPrecompileCallbackArgs {
    pub refund: Option<RefundCallArgs>,
    pub transfer_near: Option<TransferNearArgs>,
}
```

**File:** engine/src/engine.rs (L1176-1203)
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
```
