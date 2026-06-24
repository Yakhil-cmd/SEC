### Title
Global Swap Cap Reduces Effective Participation Below Minimum, Locking Transferred ICP — (`rs/sns/swap/src/swap.rs`)

### Summary

In the SNS Swap canister, `refresh_buyer_token_e8s` applies the global `max_direct_participation_icp_e8s` cap to reduce a new participant's effective contribution, then rejects the call if the capped amount falls below `min_participant_icp_e8s`. Because the ICP transfer to the swap subaccount is a separate, prior, irreversible on-chain step, the user's ICP becomes locked in the canister until the swap closes.

### Finding Description

`refresh_buyer_token_e8s` in `rs/sns/swap/src/swap.rs` executes the following sequence:

1. Reads the buyer's ICP balance from the ledger (async call).
2. Computes `max_increment_e8s = self.available_direct_participation_e8s()` — the remaining room under the global cap.
3. Caps the increment: `actual_increment_e8s = min(max_increment_e8s, requested_increment_e8s)`.
4. Caps again per-participant: `new_balance_e8s = min(new_balance_e8s, max_participant_icp_e8s)`.
5. **Rejects** if `new_balance_e8s < params.min_participant_icp_e8s`. [1](#0-0) [2](#0-1) 

When the swap is nearly full — i.e., `available_direct_participation_e8s() < min_participant_icp_e8s` — a new participant who has already transferred a valid amount (≥ `min_participant_icp_e8s`) to their swap subaccount will have their effective amount silently reduced to the small remaining capacity, causing the call to revert with:

```
"Rejecting participation of effective amount X; minimum required to participate: Y"
```

The ICP is now stranded in the swap canister's subaccount. The only recovery path is `error_refund_icp`, which is only callable after the swap closes (committed or aborted). The codebase itself acknowledges this gap with an unresolved TODO: [3](#0-2) 

The `available_direct_participation_e8s` function only tracks direct participation, not Neurons' Fund participation, so a large Neurons' Fund contribution triggered by an earlier participant can silently consume the remaining direct cap and create this condition for subsequent participants: [4](#0-3) 

The test `test_swap_cannot_finalize_via_new_participation_if_remaining_lt_minimal_participation_amount` explicitly demonstrates this revert path, confirming it is a known but unmitigated condition: [5](#0-4) 

### Impact Explanation

A user who has already made an irreversible ICP ledger transfer to the swap subaccount cannot register their participation. Their ICP is locked until the swap lifecycle ends. If the swap runs for days or weeks, the user bears opportunity cost and cannot use those funds. In a high-demand swap where the cap fills quickly, many users could be simultaneously affected. The ICP is not permanently lost, but the lock is involuntary and the user has no recourse during the swap's open period.

### Likelihood Explanation

This condition is reachable by any unprivileged user calling the public `refresh_buyer_tokens` endpoint on the SNS Swap canister. It requires no special privileges. It occurs naturally whenever a swap approaches its `max_direct_participation_icp_e8s` cap with a remaining gap smaller than `min_participant_icp_e8s` — a common end-of-swap state for popular SNS launches. The Neurons' Fund matching mechanism can accelerate this condition by consuming remaining capacity in a single step when a direct participant crosses the `min_direct_participation_threshold_icp_e8s`. [6](#0-5) 

### Recommendation

Implement the unresolved `TODO(NNS1-1682)`: when `refresh_buyer_token_e8s` determines that the effective participation amount would fall below `min_participant_icp_e8s` due to the global cap, initiate an automatic refund of the user's ICP from the swap subaccount before returning the error. This mirrors the Size protocol fix of controlling the emitted amount rather than hard-rejecting the operation. Alternatively, accept the partial amount for existing participants (where `old_amount_icp_e8s > 0`) and only enforce the minimum for first-time entries.

### Proof of Concept

```
Setup:
  max_direct_participation_icp_e8s = 500_000 ICP
  min_participant_icp_e8s          = 150_000 ICP

Step 1: User A participates with 400_000 ICP.
        → available_direct_participation_e8s() = 100_000 ICP

Step 2: User B transfers 150_000 ICP to their swap subaccount on the ICP ledger.
        (Irreversible on-chain action.)

Step 3: User B calls refresh_buyer_tokens(150_000 ICP).
        → max_increment_e8s = 100_000 (global cap remaining)
        → actual_increment_e8s = min(100_000, 150_000) = 100_000
        → new_balance_e8s = 100_000
        → 100_000 < 150_000 (min_participant_icp_e8s) → REVERT

Result: User B's 150_000 ICP is locked in the swap subaccount.
        Recovery only possible via error_refund_icp after swap closes.
``` [7](#0-6) [8](#0-7)

### Citations

**File:** rs/sns/swap/src/swap.rs (L336-346)
```rust
        pub fn validate_possibility_of_direct_participation(&self) -> Result<(), String> {
            let icp_target = self.icp_target_progress();
            if let Err(icp_target_error) = icp_target.validate() {
                log!(ERROR, "{}", icp_target_error);
            }
            if icp_target.is_reached_or_exceeded() {
                Err("The ICP target for this token swap has already been reached.".to_string())
            } else {
                Ok(())
            }
        }
```

**File:** rs/sns/swap/src/swap.rs (L1133-1133)
```rust
    /// TODO(NNS1-1682): attempt to refund ICP that cannot be accepted.
```

**File:** rs/sns/swap/src/swap.rs (L1177-1177)
```rust
        let max_increment_e8s = self.available_direct_participation_e8s();
```

**File:** rs/sns/swap/src/swap.rs (L1223-1246)
```rust
        let requested_increment_e8s = e8s - old_amount_icp_e8s;
        let actual_increment_e8s = std::cmp::min(max_increment_e8s, requested_increment_e8s);
        let new_balance_e8s = old_amount_icp_e8s.saturating_add(actual_increment_e8s);
        if new_balance_e8s > max_participant_icp_e8s {
            log!(
                INFO,
                "Participant {} contributed {} e8s - the limit per participant is {}",
                buyer,
                new_balance_e8s,
                max_participant_icp_e8s
            );
        }

        // Limit the participation based on the maximum per participant.
        let new_balance_e8s = std::cmp::min(new_balance_e8s, max_participant_icp_e8s);

        // Check that the new_balance_e8s is bigger than or equal to the minimum required for
        // participating.
        if new_balance_e8s < params.min_participant_icp_e8s {
            return Err(format!(
                "Rejecting participation of effective amount {}; minimum required to participate: {}",
                new_balance_e8s, params.min_participant_icp_e8s
            ));
        }
```

**File:** rs/sns/swap/src/swap.rs (L1522-1535)
```rust
                INFO,
                "The swap finalized successfully. \n\
                finalize_swap_response: {finalize_swap_response:?}"
            );
        }

        // Release the lock. Note, if there is a panic, the lock will
        // not be released. In that case, the Swap canister will need
        // to be upgraded to release the lock.
        self.unlock_finalize_swap();

        finalize_swap_response
    }

```

**File:** rs/sns/swap/tests/swap.rs (L4920-4969)
```rust
#[test]
fn test_refresh_buyer_tokens_not_enough_tokens_left() {
    let user1 = PrincipalId::new_user_test_id(1);
    let user2 = PrincipalId::new_user_test_id(2);
    let user3 = PrincipalId::new_user_test_id(3);
    let user4 = PrincipalId::new_user_test_id(4);

    let mut swap = SwapBuilder::new()
        .with_sns_governance_canister_id(SNS_GOVERNANCE_CANISTER_ID)
        .with_lifecycle(Open)
        .with_swap_start_due(Some(START_TIMESTAMP_SECONDS), Some(END_TIMESTAMP_SECONDS))
        .with_min_participants(1)
        .with_min_max_participant_icp(2 * E8, 40 * E8)
        .with_min_max_direct_participation(5 * E8, 100 * E8)
        .with_sns_tokens(100_000 * E8)
        .with_neuron_basket_count(3)
        .with_neurons_fund_participation()
        .build();

    let params = swap.params.unwrap();

    let amount_user1_0 = 5 * E8;
    let amount_user2_0 = 40 * E8;
    let amount_user3_0 = 40 * E8;
    let amount_user4_0 = 99 * E8 - (amount_user2_0 + amount_user3_0);

    // All tokens but one should be already bought up by users 2 to 4 --> 99 Tokens were bought
    buy_token_ok(&mut swap, &user2, &amount_user2_0, &amount_user2_0);
    buy_token_ok(&mut swap, &user3, &amount_user3_0, &amount_user3_0);
    buy_token_ok(&mut swap, &user4, &amount_user4_0, &amount_user4_0);

    // Make sure the 99 tokens were registered
    assert_eq!(
        swap.get_buyers_total().buyers_total,
        amount_user2_0 + amount_user3_0 + amount_user4_0
    );

    // Make sure that only an amount smaller than the minimum amount to be bought per user is available
    assert!(
        params.max_direct_participation_icp_e8s.unwrap() - swap.get_buyers_total().buyers_total
            < params.min_participant_icp_e8s
    );

    // No user that has not participated in the swap yet can buy this one token left
    buy_token_err(
        &mut swap,
        &user1,
        &amount_user1_0,
        "minimum required to participate",
    );
```

**File:** rs/sns/swap/tests/swap.rs (L5845-5853)
```rust
    // Operation B: User 2 attempts to participate with amount `user_2_participation_amount_icp_e8s`.
    assert_eq!(
        call_refresh_buyer_token_e8s(&mut swap, &user2, user_2_participation_amount_icp_e8s),
        Err(format!(
            "Rejecting participation of effective amount {}; minimum required to participate: {}",
            swap.available_direct_participation_e8s(),
            min_participant_icp_e8s
        ))
    );
```
