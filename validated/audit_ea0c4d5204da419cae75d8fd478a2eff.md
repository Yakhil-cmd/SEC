### Title
Unguarded NEAR `Transfer` in `exit_to_near_precompile_callback` Permanently Freezes User Funds When Target Account Does Not Exist - (File: `engine/src/contract_methods/connector.rs`)

---

### Summary

In `exit_to_near_precompile_callback`, after a successful `near_withdraw` (wNEAR unwrap), the engine issues a bare `PromiseAction::Transfer` to the user-supplied `target_account_id` with no failure callback. In NEAR Protocol, a `Transfer` action to a non-existent account fails. Because no callback is attached to detect and recover from that failure, the unwrapped NEAR is permanently stranded in the Aurora contract while the user's wNEAR ERC-20 tokens have already been irreversibly burned. This is the direct Aurora analog of M-05: a transfer to a potentially non-existent destination whose failure is never observed, causing permanent fund loss.

---

### Finding Description

The wNEAR-unwrap exit path is a three-step async promise chain:

**Step 1 — EVM execution** (`engine-precompiles/src/native.rs`, lines 587–608):
When a user calls the `ExitToNear` precompile with the `:unwrap` suffix on their wNEAR ERC-20 tokens, the EVM burns those tokens and schedules a `near_withdraw` call to the wNEAR NEP-141 contract, with `exit_to_near_precompile_callback` as the callback. The `TransferNearArgs` carrying the user-supplied `receiver_account_id` is embedded in the callback arguments. [1](#0-0) 

**Step 2 — Callback execution** (`engine/src/contract_methods/connector.rs`, lines 214–228):
`exit_to_near_precompile_callback` checks whether `near_withdraw` succeeded. If it did and `transfer_near` args are present, it creates a `PromiseBatchAction` containing a single `PromiseAction::Transfer` targeting `args.target_account_id`, then calls `handler.promise_return(promise_id)` and returns. [2](#0-1) 

**The gap**: No second-level callback is attached to the `Transfer` promise. `promise_return` merely designates the promise as the function's return value; it does not add any error-handling logic. If the `Transfer` action fails, execution ends silently with no refund path.

**Step 3 — Failure scenario**:
In NEAR Protocol, a `Transfer` action to an account that does not exist fails at the runtime level. The NEAR that was unwrapped by `near_withdraw` (and is now held by the Aurora contract) is not automatically returned to any user-controlled address. The `error_refund` feature only guards against failure of the *base* promise (`near_withdraw`), not against failure of the *Transfer* promise created inside the callback. [3](#0-2) 

The `ExitToNearPrecompileCallbackArgs` struct confirms that `refund` and `transfer_near` are independent optional fields — there is no refund path wired to a failed `transfer_near`. [4](#0-3) 

---

### Impact Explanation

**Permanent freezing of funds (Critical).**

When the `Transfer` to a non-existent NEAR account fails:
- The user's wNEAR ERC-20 tokens are already burned in the EVM — this is irreversible.
- The NEAR unwrapped by `near_withdraw` is held by the Aurora contract with no recovery mechanism.
- There is no admin function, no second callback, and no re-entry point that credits the stranded NEAR back to the user.

The funds are permanently frozen inside the Aurora contract.

---

### Likelihood Explanation

The `target_account_id` is entirely user-supplied calldata passed through the EVM precompile input. Two realistic triggering conditions exist:

1. **Typo / user error**: A user mistypes their NEAR account ID. NEAR account IDs are validated for format (e.g., `AccountId::try_from`) but not for on-chain existence at precompile invocation time.
2. **Deleted account**: A user specifies a NEAR account that exists when they submit the EVM transaction but is deleted (via `DeleteAccount`) before the async `Transfer` receipt executes. NEAR's asynchronous execution model makes this a non-negligible window.

No privileged access is required. Any EVM user holding wNEAR ERC-20 tokens can trigger this path.

---

### Recommendation

Attach a second-level failure callback to the `Transfer` promise created inside `exit_to_near_precompile_callback`. That callback should detect a failed transfer result and invoke `refund_on_error` (or an equivalent EVM credit) to restore the user's wNEAR ERC-20 balance. Concretely, replace the bare `promise_return` with a `PromiseWithCallbackArgs` whose callback re-mints the wNEAR ERC-20 tokens to the original sender on failure, mirroring the existing `error_refund` logic already used for the `near_withdraw` failure case. [5](#0-4) 

---

### Proof of Concept

1. Alice holds 100 wNEAR ERC-20 tokens on Aurora.
2. Alice calls the `ExitToNear` precompile with `receiver_account_id = "typo-account.near"` (a non-existent NEAR account) and the `:unwrap` suffix.
3. The EVM burns Alice's 100 wNEAR ERC-20 tokens and schedules `near_withdraw(amount: 100)` on the wNEAR NEP-141 contract, with `exit_to_near_precompile_callback` as the callback.
4. `near_withdraw` succeeds: 100 NEAR is transferred from the wNEAR contract to the Aurora contract.
5. `exit_to_near_precompile_callback` fires, sees `PromiseResult::Successful`, and issues `PromiseBatchAction { target_account_id: "typo-account.near", actions: [Transfer { amount: 100 NEAR }] }`.
6. The NEAR runtime rejects the `Transfer` because `"typo-account.near"` does not exist.
7. No callback is present to observe the failure. The 100 NEAR remains in the Aurora contract.
8. Alice has lost 100 wNEAR ERC-20 tokens and received 0 NEAR. The funds are permanently frozen. [6](#0-5) [7](#0-6)

### Citations

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

**File:** engine/src/contract_methods/connector.rs (L214-230)
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

**File:** engine-types/src/parameters/connector.rs (L129-134)
```rust
/// Arguments for callback used in the `exit_to_near` precompile.
#[derive(Debug, Clone, BorshSerialize, BorshDeserialize, PartialEq, Eq, Default)]
pub struct ExitToNearPrecompileCallbackArgs {
    pub refund: Option<RefundCallArgs>,
    pub transfer_near: Option<TransferNearArgs>,
}
```
