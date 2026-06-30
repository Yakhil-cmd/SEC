### Title
Permanent Fund Loss in wNEAR Unwrap Flow Due to Unhandled NEAR Transfer Failure - (File: engine/src/contract_methods/connector.rs)

### Summary
When a user unwraps wNEAR ERC-20 tokens via the `ExitToNear` precompile with the `:unwrap` suffix, the ERC-20 tokens are burned atomically in the EVM, then a `near_withdraw` cross-contract call is scheduled. If `near_withdraw` succeeds but the subsequent NEAR push-transfer to the user-specified `target_account_id` fails (e.g., the account does not exist and the amount is below NEAR's minimum balance), the ERC-20 tokens are permanently burned with no recovery mechanism. The NEAR is silently returned to Aurora's account.

### Finding Description

The wNEAR unwrap flow is initiated in `exit_erc20_token_to_near` when the `:unwrap` suffix is detected:

```rust
Some(Message::UnwrapWnear) if erc20_address == get_wnear_address(io).raw() => {
    (
        nep141_account_id,
        format!(r#"{{"amount":"{}"}}"#, exit_params.amount.as_u128()),
        "near_withdraw",
        Some(TransferNearArgs {
            target_account_id: exit_params.receiver_account_id.clone(),
            amount: exit_params.amount.as_u128(),
        }),
        ...
    )
}
``` [1](#0-0) 

The ERC-20 tokens are burned inside the EVM execution (committed atomically), and a `near_withdraw` promise is scheduled with `exit_to_near_precompile_callback` as its callback. Inside the callback, when `near_withdraw` succeeds, the code creates a NEAR `Transfer` batch promise and immediately returns it — with **no further callback** to handle its failure:

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
        let promise_id = handler.promise_create_batch(&promise);
        handler.promise_return(promise_id);
    }
    None
``` [2](#0-1) 

The `error_refund` mechanism only handles the case where `near_withdraw` itself fails:

```rust
} else if let Some(args) = args.refund {
    // Exit call failed; need to refund tokens
    let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;
``` [3](#0-2) 

There is no analogous recovery path for the case where `near_withdraw` succeeds but the subsequent NEAR transfer to `target_account_id` fails. The `TransferNearArgs` struct carries only the destination and amount — no refund address for this second leg: [4](#0-3) 

### Impact Explanation

**High — Permanent freezing of user funds.**

The ERC-20 token burn is committed in the EVM state during the original NEAR transaction. If the NEAR transfer in the callback fails, the NEAR is returned to Aurora's account by the NEAR runtime, but there is no on-chain mechanism to re-mint the burned ERC-20 tokens or return the NEAR to the user. The user permanently loses their wNEAR-backed ERC-20 tokens, and the corresponding NEAR is stranded in Aurora's account with no user-accessible recovery path.

### Likelihood Explanation

**Low.**

In NEAR Protocol, a `Transfer` action to a non-existent account fails if the transferred amount is below the minimum account balance (~0.00182 NEAR / 1.82 milliNEAR). A user unwrapping a dust amount of wNEAR ERC-20 tokens while specifying a non-existent NEAR account as the recipient triggers this failure. Additionally, if the recipient account is deleted between transaction submission and callback execution (a valid NEAR operation), the transfer also fails. Both conditions are reachable by an unprivileged user interacting with the `ExitToNear` precompile.

### Recommendation

Attach a second-level callback to the NEAR transfer promise created inside `exit_to_near_precompile_callback`. If the NEAR transfer fails, this callback should re-mint the burned ERC-20 tokens (or credit the equivalent ETH balance) to the user's EVM address — mirroring the existing `refund_on_error` logic used for the `near_withdraw` failure case. Alternatively, adopt a pull-over-push pattern: instead of pushing NEAR to the user, credit the unwrapped NEAR to a claimable balance that the user can withdraw in a separate transaction.

### Proof of Concept

1. User holds wNEAR ERC-20 tokens on Aurora (e.g., 1000 yoctoNEAR worth, below the 1.82 milliNEAR minimum account balance).
2. User calls the `ExitToNear` precompile with the `:unwrap` suffix, specifying a non-existent NEAR account (e.g., `"ghost.near"`) as `receiver_account_id`.
3. The EVM burns the wNEAR ERC-20 tokens — this state change is committed.
4. `near_withdraw` is called on the wNEAR NEP-141 contract; it succeeds, transferring the NEAR to Aurora's account.
5. `exit_to_near_precompile_callback` fires: `near_withdraw` succeeded, so it schedules `PromiseAction::Transfer { amount: 1000 yoctoNEAR }` to `"ghost.near"` with no failure callback.
6. The NEAR transfer fails: `"ghost.near"` does not exist and 1000 yoctoNEAR < minimum balance. NEAR runtime returns the NEAR to Aurora's account.
7. No further callback fires. The user's wNEAR ERC-20 tokens are permanently burned. The 1000 yoctoNEAR is stranded in Aurora's account. The user has no on-chain recourse.

### Citations

**File:** engine-precompiles/src/native.rs (L587-608)
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
                events::ExitToNear::Legacy(ExitToNearLegacy {
                    sender: Address::new(erc20_address),
                    erc20_address: Address::new(erc20_address),
                    dest: exit_params.receiver_account_id.to_string(),
                    amount: exit_params.amount,
                }),
            )
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

**File:** engine/src/contract_methods/connector.rs (L231-237)
```rust
        } else if let Some(args) = args.refund {
            // Exit call failed; need to refund tokens
            let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;

            if !refund_result.status.is_ok() {
                return Err(errors::ERR_REFUND_FAILURE.into());
            }
```

**File:** engine-types/src/parameters/connector.rs (L122-127)
```rust
/// Arguments for `ft_transfer` transaction.
#[derive(Debug, Clone, BorshSerialize, BorshDeserialize, PartialEq, Eq)]
pub struct TransferNearArgs {
    pub target_account_id: AccountId,
    pub amount: u128,
}
```
