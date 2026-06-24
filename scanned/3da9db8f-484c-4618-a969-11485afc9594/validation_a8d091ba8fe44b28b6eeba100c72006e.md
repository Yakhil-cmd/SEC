### Title
ICP Temporarily Stuck in SNS Swap Subaccount When `refresh_buyer_tokens` Fails During OPEN State - (File: rs/sns/swap/src/swap.rs)

### Summary
A user who transfers ICP to the SNS Swap canister's per-principal subaccount and then calls `refresh_buyer_tokens` can have their ICP permanently locked for the duration of the swap's OPEN lifecycle (up to 90 days) if the call fails for any reason. The only recovery path, `error_refund_icp`, is gated behind the swap reaching COMMITTED or ABORTED state. No withdrawal mechanism exists during the OPEN state.

### Finding Description

The SNS Swap canister implements a two-step participation flow:

1. The user transfers ICP to `subaccount(swap_canister, P)` on the ICP ledger.
2. The user calls `refresh_buyer_tokens` to notify the swap of the transfer.

`refresh_buyer_token_e8s` in `rs/sns/swap/src/swap.rs` enforces several conditions that can cause the call to fail after the ICP has already been transferred:

- The transferred amount is below `min_participant_icp_e8s`: [1](#0-0) 

- The swap has reached `MAX_NEURONS_FOR_DIRECT_PARTICIPANTS` (new participant rejected): [2](#0-1) 

- The swap's direct participation target is already full (`max_increment_e8s == 0`): [3](#0-2) 

When any of these conditions trigger, the ICP remains in the user's subaccount on the ICP ledger, held by the swap canister. The only recovery function is `error_refund_icp`, which is explicitly gated: [4](#0-3) 

This means during the OPEN lifecycle — which can last up to 90 days per the `SetOpenTimeWindowRequest` constraint — there is no mechanism for the user to recover their ICP. The code itself acknowledges this gap with an unresolved TODO: [5](#0-4) 

The canister-level endpoint exposes this directly to any unprivileged caller: [6](#0-5) 

### Impact Explanation

Any user who sends ICP to the swap subaccount and whose `refresh_buyer_tokens` call fails (due to amount below minimum, participant cap reached, or ICP target full) has their ICP locked in the swap canister's subaccount for the entire remaining duration of the OPEN state. Depending on swap configuration, this can be up to 90 days. The user cannot transfer, withdraw, or recover those funds through any on-chain mechanism until the swap reaches COMMITTED or ABORTED. This is a direct ledger conservation / temporarily stuck funds impact on real user assets.

### Likelihood Explanation

This is reachable by any unprivileged ingress sender. Common realistic triggers:

1. A user sends slightly less ICP than `min_participant_icp_e8s` (e.g., due to ledger transfer fees being deducted before the balance is read).
2. A user participates late when the swap's direct participation cap is already full — `refresh_buyer_tokens` returns an error but the ICP is already in the subaccount.
3. A user is a new participant when `MAX_NEURONS_FOR_DIRECT_PARTICIPANTS` is reached.

All three scenarios are realistic in production SNS swaps with active participation.

### Recommendation

Implement an immediate refund path within `refresh_buyer_token_e8s` itself: when the call would fail after the ICP balance is confirmed on-chain, the function should attempt to transfer the ICP back to the user's principal before returning the error. This is exactly what the existing TODO (`NNS1-1682`) calls for: [7](#0-6) 

Alternatively, expose a `withdraw_icp` endpoint callable during the OPEN state that allows a user to reclaim ICP from their subaccount if it has not been accepted into `buyers` state, analogous to `error_refund_icp` but without the lifecycle restriction.

### Proof of Concept

1. SNS swap is in OPEN state with `min_participant_icp_e8s = 1_000_000_000` (10 ICP).
2. User transfers `999_999_990` e8s (just below minimum, after ledger fee) to `subaccount(swap_canister, user_principal)` on the ICP ledger.
3. User calls `refresh_buyer_tokens { buyer: user_principal }`.
4. Inside `refresh_buyer_token_e8s`, the balance check reads `e8s = 999_999_990 < min_participant_icp_e8s = 1_000_000_000` and returns `Err(...)`: [1](#0-0) 
5. User attempts `error_refund_icp { source_principal_id: user_principal }`.
6. The call returns `ErrorRefundIcpResponse::new_precondition_error("Error refunds can only be performed when the swap is ABORTED or COMMITTED")`: [4](#0-3) 
7. The ICP remains locked in the subaccount for the remainder of the OPEN period (up to 90 days), with no on-chain recovery path available to the user.

### Citations

**File:** rs/sns/swap/src/swap.rs (L1128-1133)
```rust
    /// If a ledger transfer was successfully made, but this call
    /// fails (many reasons are possible), the owner of the ICP sent
    /// to the subaccount can reclaim their tokens using `error_refund_icp`
    /// once this swap is closed (committed or aborted).
    ///
    /// TODO(NNS1-1682): attempt to refund ICP that cannot be accepted.
```

**File:** rs/sns/swap/src/swap.rs (L1177-1177)
```rust
        let max_increment_e8s = self.available_direct_participation_e8s();
```

**File:** rs/sns/swap/src/swap.rs (L1187-1197)
```rust
            if (num_direct_participants + 1) * num_sns_neurons_per_basket
                > MAX_NEURONS_FOR_DIRECT_PARTICIPANTS
            {
                return Err(format!(
                    "The swap has reached the maximum number of direct participants ({num_direct_participants}) and does \
                     not accept new participants; existing participants may still increase their \
                     ICP participation amount. This constraint ensures that SNS neuron baskets can \
                     be created for all existing participants (SNS neuron basket size: {num_sns_neurons_per_basket}, \
                     MAX_NEURONS_FOR_DIRECT_PARTICIPANTS: {MAX_NEURONS_FOR_DIRECT_PARTICIPANTS}).",
                ));
            }
```

**File:** rs/sns/swap/src/swap.rs (L1202-1207)
```rust
        if e8s < params.min_participant_icp_e8s {
            return Err(format!(
                "Amount transferred: {}; minimum required to participate: {}",
                e8s, params.min_participant_icp_e8s
            ));
        }
```

**File:** rs/sns/swap/src/swap.rs (L1931-1936)
```rust
        // Fail if the request is premature.
        if !(self.lifecycle() == Lifecycle::Aborted || self.lifecycle() == Lifecycle::Committed) {
            return ErrorRefundIcpResponse::new_precondition_error(
                "Error refunds can only be performed when the swap is ABORTED or COMMITTED",
            );
        }
```

**File:** rs/sns/swap/canister/canister.rs (L127-143)
```rust
#[update]
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    log!(INFO, "refresh_buyer_tokens");
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()
    };
    let icp_ledger = create_real_icp_ledger(swap().init_or_panic().icp_ledger_or_panic());
    match swap_mut()
        .refresh_buyer_token_e8s(p, arg.confirmation_text, this_canister_id(), &icp_ledger)
        .await
    {
        Ok(r) => r,
        Err(msg) => panic!("{}", msg),
    }
}
```
