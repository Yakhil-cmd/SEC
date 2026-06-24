Audit Report

## Title
SNS Swap `refresh_buyer_token_e8s` Race Condition Temporarily Locks Participant ICP Without Participation - (File: rs/sns/swap/src/swap.rs)

## Summary
The SNS swap canister's two-step participation flow contains a real, demonstrable race condition at the `await` point of the ICP ledger balance query. A participant who transfers ICP to their swap subaccount can have those funds locked in the subaccount — unregistered in `self.buyers` and irrecoverable via `error_refund_icp` — for the entire remaining swap duration if another participant fills the remaining capacity during the inter-canister call. The codebase explicitly acknowledges this with `TODO(NNS1-1682)`.

## Finding Description

`refresh_buyer_token_e8s` performs a pre-await check at lines 1144–1147 and a post-await check at lines 1168–1171 for both lifecycle state and ICP target capacity: [1](#0-0) 

Between these two checks, the function unconditionally awaits a cross-canister call to the ICP ledger: [2](#0-1) 

This `await` is a guaranteed yield point in the IC execution model. Any other canister message — including another participant's `refresh_buyer_tokens` — can execute during this window. If that message fills the remaining `available_direct_participation_e8s()`, the post-await `validate_possibility_of_direct_participation()` call at line 1170 returns an error and the function exits without writing to `self.buyers`: [3](#0-2) 

A second failure path exists even if `validate_possibility_of_direct_participation()` passes: if the remaining capacity is smaller than `params.min_participant_icp_e8s`, the check at line 1241 rejects the participation: [4](#0-3) 

In both cases, the participant's ICP sits in their swap subaccount but is not registered. The participant cannot retry `refresh_buyer_tokens` (swap is full) and cannot call `error_refund_icp` because that endpoint is hard-gated to `Aborted` or `Committed` lifecycle states: [5](#0-4) 

The docstring and an open TODO on the function itself confirm this is a known, unresolved issue: [6](#0-5) 

## Impact Explanation

User ICP is temporarily but concretely locked — inaccessible for any purpose — for the entire remaining duration of the swap. The maximum lock duration is bounded by `swap_due_timestamp_seconds`, which can be days to weeks. The ICP is not permanently lost but is fully illiquid during this period. This constitutes a significant SNS security impact with concrete user harm, matching the High bounty category: "Significant... SNS... security impact with concrete user or protocol harm."

## Likelihood Explanation

No malicious actor, no privileged access, and no special tooling are required. The IC execution model guarantees that every `await` is a yield point. Any concurrent `refresh_buyer_tokens` message scheduled during the ledger balance query window that fills the remaining capacity triggers the condition. Popular swaps operating near their `max_direct_participation_icp_e8s` ceiling — exactly the swaps with the most concurrent participation — are at highest risk. This is a structural property of the two-step flow, not a theoretical edge case.

## Recommendation

1. Add a `min_accepted_icp_e8s: Option<u64>` field to `RefreshBuyerTokensRequest`. After computing `new_balance_e8s` and `actual_increment_e8s`, if `actual_increment_e8s` is less than the caller-supplied minimum, return an error before writing any state, giving participants slippage-protection analogous to `amountOutMin` in AMM protocols.
2. Resolve `TODO(NNS1-1682)` by attempting an immediate ICP refund within the same call when the participation cannot be accepted (i.e., when `new_balance_e8s < params.min_participant_icp_e8s` or when `validate_possibility_of_direct_participation` fails post-await), rather than deferring recovery to post-close `error_refund_icp`.

## Proof of Concept

```
Setup: max_direct_participation_icp_e8s = 10 ICP, min_participant_icp_e8s = 1 ICP.
State: 9 ICP committed. 1 ICP remaining.

1. Alice transfers 1 ICP to her swap subaccount (irreversible ledger op).
2. Alice calls refresh_buyer_tokens.
3. Pre-await validate_possibility_of_direct_participation() passes (1 ICP remaining).
4. Swap canister awaits icp_ledger.account_balance() — yield point.
5. Bob's refresh_buyer_tokens executes, commits the final 1 ICP.
   available_direct_participation_e8s() == 0.
6. Alice's call resumes. Post-await validate_possibility_of_direct_participation() returns Err.
7. refresh_buyer_token_e8s returns Err. self.buyers unchanged.
8. Alice calls refresh_buyer_tokens again → pre-await check fails (swap full).
9. Alice calls error_refund_icp → "Error refunds can only be performed when the swap
   is ABORTED or COMMITTED".
10. Alice's 1 ICP is locked until swap_due_timestamp_seconds elapses.

Reproducible as a PocketIC integration test: open a swap with 1 ICP remaining,
pause execution at the ledger balance query await point, inject a second
refresh_buyer_tokens call that fills capacity, resume Alice's call, assert
error_refund_icp is rejected and Alice's subaccount balance is non-zero.
```

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

**File:** rs/sns/swap/src/swap.rs (L1143-1147)
```rust
        // These two checks need to be repeated after awaiting the response from the ICP ledger.
        self.validate_lifecycle_is_open()
            .map_err(context_before_awaiting_icp_ledger_response)?;
        self.validate_possibility_of_direct_participation()
            .map_err(context_before_awaiting_icp_ledger_response)?;
```

**File:** rs/sns/swap/src/swap.rs (L1158-1163)
```rust
            icp_ledger
                .account_balance(account)
                .await
                .map_err(|x| x.to_string())?
                .get_e8s()
        };
```

**File:** rs/sns/swap/src/swap.rs (L1168-1171)
```rust
        self.validate_lifecycle_is_open()
            .map_err(context_after_awaiting_icp_ledger_response)?;
        self.validate_possibility_of_direct_participation()
            .map_err(context_after_awaiting_icp_ledger_response)?;
```

**File:** rs/sns/swap/src/swap.rs (L1241-1246)
```rust
        if new_balance_e8s < params.min_participant_icp_e8s {
            return Err(format!(
                "Rejecting participation of effective amount {}; minimum required to participate: {}",
                new_balance_e8s, params.min_participant_icp_e8s
            ));
        }
```

**File:** rs/sns/swap/src/swap.rs (L1932-1935)
```rust
        if !(self.lifecycle() == Lifecycle::Aborted || self.lifecycle() == Lifecycle::Committed) {
            return ErrorRefundIcpResponse::new_precondition_error(
                "Error refunds can only be performed when the swap is ABORTED or COMMITTED",
            );
```
