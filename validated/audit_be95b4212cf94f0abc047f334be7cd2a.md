Audit Report

## Title
ICP Locked in Swap Subaccount With No Recovery Path While Swap Is OPEN - (File: rs/sns/swap/src/swap.rs)

## Summary

The SNS Swap canister's two-step participation flow leaves a buyer's ICP stranded in their principal-derived subaccount when `refresh_buyer_token_e8s` rejects the registration (swap cap reached, per-participant cap exceeded, or other post-transfer errors). The sole recovery function, `error_refund_icp`, unconditionally rejects calls while the swap is `Open`, leaving the ICP inaccessible for the entire remaining open period — up to 90 days. If the swap subsequently commits, the buyer's unregistered ICP is returned but they receive no SNS tokens in exchange.

## Finding Description

`refresh_buyer_token_e8s` (rs/sns/swap/src/swap.rs, L1134) performs two rounds of lifecycle and capacity checks:

- Pre-await: L1144–1147 calls `validate_lifecycle_is_open()` and `validate_possibility_of_direct_participation()`.
- Post-await: L1168–1171 repeats both checks after the async ICP ledger balance query returns.

If `validate_possibility_of_direct_participation` fails at either point (because `available_direct_participation_e8s()` returns 0), the function returns `Err(...)` without updating `self.buyers`. The buyer's ICP is already sitting in `Account { owner: swap_canister_id, subaccount: principal_to_subaccount(buyer) }` on the ICP ledger, but no `BuyerState` is created or updated.

Additionally, at L1236–1237, even when the swap is not full, a buyer who sends more than `max_participant_icp_e8s` has only the capped amount recorded; the excess remains in the subaccount and is equally unreachable.

The only recovery path is `error_refund_icp` (L1925), which at L1932–1935 unconditionally returns a precondition error unless the lifecycle is `Aborted` or `Committed`. There is no alternative endpoint callable while the swap is `Open`.

The code itself documents this gap at L1128–1133 with an explicit TODO(NNS1-1682).

## Impact Explanation

This matches the Medium bounty impact: **moderate user-funds/security impact**. A user's ICP is inaccessible for up to 90 days with no on-chain mechanism to reclaim it early. If the swap commits while the ICP is stranded, the buyer recovers their ICP but receives zero SNS tokens — a concrete financial harm. The funds are not permanently lost, which prevents this from reaching the Critical/High threshold, but the extended, involuntary illiquidity and potential loss of swap participation opportunity constitute a meaningful, demonstrable harm to users of the SNS financial protocol.

## Likelihood Explanation

Reachable by any unprivileged user with no special access. The two-step flow (ledger transfer → `refresh_buyer_tokens`) is the standard documented participation path. A race condition where the swap fills between the transfer and the notify call is realistic during a popular SNS launch. A user who accidentally or intentionally sends more than `max_participant_icp_e8s` also hits the excess-locking path. No governance majority, subnet majority, or privileged access is required.

## Recommendation

Implement TODO(NNS1-1682): when `refresh_buyer_token_e8s` determines that none or only part of the transferred ICP can be accepted, immediately initiate an async refund of the unacceptable portion back to the buyer's principal account on the ICP ledger, rather than leaving it stranded. Alternatively, expose a `refund_unaccepted_icp` endpoint callable while the swap is `Open` that checks whether the caller's subaccount balance exceeds their registered `BuyerState.icp.amount_e8s` and refunds the difference.

## Proof of Concept

1. Deploy an SNS swap in `Open` state with `available_direct_participation_e8s()` returning 0 (direct participation cap fully reached).
2. Buyer calls ICP ledger `transfer` to `Account { owner: swap_canister_id, subaccount: principal_to_subaccount(buyer) }` for 5 ICP.
3. Buyer calls `refresh_buyer_tokens`. `validate_possibility_of_direct_participation` fails post-await at L1170–1171; function returns `Err(...)`. `self.buyers` is not updated.
4. Buyer calls `error_refund_icp`. Returns `ErrorRefundIcpResponse::new_precondition_error("Error refunds can only be performed when the swap is ABORTED or COMMITTED")` (L1932–1935).
5. Buyer's 5 ICP remains locked in the subaccount for the remainder of the open period. If the swap later commits, `error_refund_icp` returns the ICP but no SNS tokens are issued to the buyer.

A deterministic integration test using PocketIC can reproduce this by: opening a swap, filling it to capacity via a first buyer, having a second buyer transfer ICP and call `refresh_buyer_tokens` (expecting `Err`), then asserting that `error_refund_icp` returns a precondition error and the subaccount balance remains non-zero. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** rs/sns/swap/src/swap.rs (L1144-1147)
```rust
        self.validate_lifecycle_is_open()
            .map_err(context_before_awaiting_icp_ledger_response)?;
        self.validate_possibility_of_direct_participation()
            .map_err(context_before_awaiting_icp_ledger_response)?;
```

**File:** rs/sns/swap/src/swap.rs (L1168-1171)
```rust
        self.validate_lifecycle_is_open()
            .map_err(context_after_awaiting_icp_ledger_response)?;
        self.validate_possibility_of_direct_participation()
            .map_err(context_after_awaiting_icp_ledger_response)?;
```

**File:** rs/sns/swap/src/swap.rs (L1236-1237)
```rust
        // Limit the participation based on the maximum per participant.
        let new_balance_e8s = std::cmp::min(new_balance_e8s, max_participant_icp_e8s);
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
