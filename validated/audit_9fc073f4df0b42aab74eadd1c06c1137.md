### Title
SNS Swap Capacity Permanently Locked When Remaining Participation Falls Below Per-Participant Minimum - (`rs/sns/swap/src/swap.rs`)

### Summary

The `refresh_buyer_token_e8s` function in the SNS Swap canister enforces both a per-participant minimum (`min_participant_icp_e8s`) and a global capacity ceiling (`max_direct_participation_icp_e8s`). When the remaining direct-participation capacity falls below `min_participant_icp_e8s` and all existing participants are already at `max_participant_icp_e8s`, the remaining capacity becomes permanently inaccessible: new participants are rejected because their effective contribution (capped to the remaining space) falls below the minimum, and existing participants at the per-participant ceiling cannot increase their stake further.

### Finding Description

`refresh_buyer_token_e8s` applies three sequential constraints:

1. **Pre-cap minimum check** (line 1202): rejects any caller whose ledger balance is below `min_participant_icp_e8s`.
2. **Capacity cap** (line 1224): silently reduces the accepted increment to `available_direct_participation_e8s()`.
3. **Post-cap minimum check** (line 1241): rejects the participation if the *effective* new balance (after the cap) is below `min_participant_icp_e8s`.
4. **Per-participant ceiling** (line 1237): clamps `new_balance_e8s` to `max_participant_icp_e8s`. [1](#0-0) 

When `available_direct_participation_e8s()` < `min_participant_icp_e8s`:

- A **new participant** who deposits exactly `min_participant_icp_e8s` passes the pre-cap check, but their increment is capped to the smaller remaining amount. The post-cap check then rejects them because `new_balance_e8s < min_participant_icp_e8s`.
- An **existing participant** whose `old_amount_icp_e8s` is already ≥ `min_participant_icp_e8s` can top up by any amount, because the post-cap check compares the *total* balance, not the increment. However, if that participant is already at `max_participant_icp_e8s`, the per-participant ceiling (line 1237) clamps their new balance back to `max_participant_icp_e8s`, producing no net change. [2](#0-1) 

The `available_direct_participation_e8s` helper simply subtracts current from maximum: [3](#0-2) 

**Concrete deadlock scenario:**

| Parameter | Value |
|---|---|
| `max_direct_participation_icp_e8s` | 1,050,000 ICP |
| `max_participant_icp_e8s` | 500,000 ICP |
| `min_participant_icp_e8s` | 100,000 ICP |

- User A participates with 500,000 ICP (at per-participant ceiling).
- User B participates with 500,000 ICP (at per-participant ceiling).
- Total committed: 1,000,000 ICP. Remaining: **50,000 ICP**.
- User C (new) deposits 100,000 ICP → passes pre-cap check, increment capped to 50,000, post-cap check fails (`50,000 < 100,000`). **Rejected.**
- User A tries to top up: `new_balance = min(500,000 + 50,000, 500,000) = 500,000` — no change. **No-op.**
- User B: same. **No-op.**
- **50,000 ICP of capacity is permanently locked.**

The existing test `test_swap_cannot_finalize_via_new_participation_if_remaining_lt_minimal_participation_amount` covers the case where an existing participant below the ceiling can still fill the gap, but does not cover the case where all participants are at `max_participant_icp_e8s`. [4](#0-3) 

### Impact Explanation

- The SNS Swap can never reach its `max_direct_participation_icp_e8s` target, preventing early finalization.
- If the locked remainder is also needed to cross `min_direct_participation_icp_e8s`, the swap will abort at the deadline despite having nearly sufficient participation.
- ICP transferred to the swap subaccounts by would-be participants is locked until the swap closes and `error_refund_icp` is called, creating a temporary denial-of-funds.
- The swap's SNS token price is determined by `total_direct_participation / sns_token_e8s`; a permanently sub-target participation distorts the final token distribution.

### Likelihood Explanation

The condition arises whenever `max_direct_participation_icp_e8s mod max_participant_icp_e8s < min_participant_icp_e8s`. These three parameters are set at SNS initialization via an NNS governance proposal and cannot be changed after the swap opens. Any SNS launch whose parameters satisfy this arithmetic relationship will hit the deadlock once enough participants fill the swap. A sophisticated actor who monitors the swap state can deliberately be the last participant to bring the total to exactly `max_direct - r` (where `r < min_participant`), intentionally locking the remaining capacity. [5](#0-4) [6](#0-5) 

### Recommendation

After computing `actual_increment_e8s` and before the post-cap minimum check, add a guard that allows an existing participant to fill the remaining capacity even if the increment is below `min_participant_icp_e8s`, provided their resulting total remains ≥ `min_participant_icp_e8s`. For new participants, when `max_increment_e8s < min_participant_icp_e8s`, the function should return a descriptive error rather than silently capping and then rejecting. Analogously, the `compute_participation_increment` function already implements a correct guard:

```rust
if user_participation.saturating_add(max_available_increment) < min_user_participation {
    return Err((0, 0));
}
``` [7](#0-6) 

The same logic should be applied in `refresh_buyer_token_e8s`: if `old_amount_icp_e8s == 0` (new participant) and `max_increment_e8s < min_participant_icp_e8s`, reject immediately with a clear error. If `old_amount_icp_e8s > 0` (existing participant), allow the top-up as long as `new_balance_e8s >= min_participant_icp_e8s`.

### Proof of Concept

```
Swap parameters:
  max_direct_participation_icp_e8s = 1_050_000 * E8
  max_participant_icp_e8s          =   500_000 * E8
  min_participant_icp_e8s          =   100_000 * E8

Step 1: User A calls refresh_buyer_tokens with ledger balance 500_000 * E8.
        → accepted, buyer state = 500_000 * E8.

Step 2: User B calls refresh_buyer_tokens with ledger balance 500_000 * E8.
        → accepted, buyer state = 500_000 * E8.
        → available_direct_participation_e8s() = 50_000 * E8.

Step 3: User C (new) transfers 100_000 * E8 to their swap subaccount,
        then calls refresh_buyer_tokens.
        → e8s = 100_000 * E8 ≥ min_participant_icp_e8s ✓ (pre-cap check passes)
        → actual_increment = min(50_000, 100_000) = 50_000 * E8
        → new_balance = 50_000 * E8 < min_participant_icp_e8s (100_000)
        → ERROR: "Rejecting participation of effective amount 50000e8s;
                  minimum required to participate: 100000e8s"

Step 4: User A transfers 50_000 * E8 more, calls refresh_buyer_tokens.
        → new_balance = min(500_000 + 50_000, max_participant=500_000) = 500_000
        → no change in buyer state; 50_000 ICP permanently locked.

Result: 50_000 * E8 ICP of swap capacity is permanently inaccessible.
        The swap cannot reach max_direct_participation_icp_e8s = 1_050_000 * E8.
``` [1](#0-0) [8](#0-7)

### Citations

**File:** rs/sns/swap/src/swap.rs (L514-535)
```rust
    pub fn max_direct_participation_e8s(&self) -> u64 {
        self.params
            .expect("Expected params to be set")
            .max_direct_participation_icp_e8s
            .expect("Expected params.max_direct_participation_icp_e8s to be set")
    }

    /// The amount of ICP e8s currently available for direct participation.
    pub fn available_direct_participation_e8s(&self) -> u64 {
        let max_direct_participation_e8s = self.max_direct_participation_e8s();
        let current_direct_participation_e8s = self.current_direct_participation_e8s();
        max_direct_participation_e8s
            .checked_sub(current_direct_participation_e8s)
            .unwrap_or_else(|| {
                log!(
                    ERROR,
                    "max_direct_participation_e8s ({max_direct_participation_e8s}) \
                    < current_direct_participation_e8s ({current_direct_participation_e8s})"
                );
                0
            })
    }
```

**File:** rs/sns/swap/src/swap.rs (L1177-1177)
```rust
        let max_increment_e8s = self.available_direct_participation_e8s();
```

**File:** rs/sns/swap/src/swap.rs (L1200-1246)
```rust
        // Check that the minimum amount has been transferred before
        // actually creating an entry for the buyer.
        if e8s < params.min_participant_icp_e8s {
            return Err(format!(
                "Amount transferred: {}; minimum required to participate: {}",
                e8s, params.min_participant_icp_e8s
            ));
        }
        let max_participant_icp_e8s = params.max_participant_icp_e8s;

        let old_amount_icp_e8s = self
            .buyers
            .get(&buyer.to_string())
            .map_or(0, |buyer| buyer.amount_icp_e8s());

        if old_amount_icp_e8s >= e8s {
            // Already up-to-date. Strict inequality can happen if messages are re-ordered.
            return Ok(RefreshBuyerTokensResponse {
                icp_accepted_participation_e8s: old_amount_icp_e8s,
                icp_ledger_account_balance_e8s: e8s,
            });
        }
        // Subtraction safe because of the preceding if-statement.
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

**File:** rs/sns/swap/src/swap.rs (L3237-3242)
```rust
    // Check that the user can reach min_user_participation with the next
    // ticket. We do not want users to participate less than min_user_participation
    // even if that's what's remaining in the swap.
    if user_participation.saturating_add(max_available_increment) < min_user_participation {
        return Err((0, 0));
    }
```

**File:** rs/sns/swap/tests/swap.rs (L5706-5865)
```rust
#[test]
fn test_swap_cannot_finalize_via_new_participation_if_remaining_lt_minimal_participation_amount() {
    let user1 = PrincipalId::new_user_test_id(1);
    let user2 = PrincipalId::new_user_test_id(2);
    let call_refresh_buyer_token_e8s = |swap: &mut Swap,
                                        user: &PrincipalId,
                                        icp_ledger_account_balance_e8s: u64|
     -> Result<RefreshBuyerTokensResponse, String> {
        swap.refresh_buyer_token_e8s(
            *user,
            None,
            SWAP_CANISTER_ID,
            &mock_stub(vec![LedgerExpect::AccountBalance(
                Account {
                    owner: SWAP_CANISTER_ID.get().into(),
                    subaccount: Some(principal_to_subaccount(user)),
                },
                Ok(Tokens::from_e8s(icp_ledger_account_balance_e8s)),
            )]),
        )
        .now_or_never()
        .unwrap()
    };

    // The amount that will be participated by user 1 at the beginning.
    let user_1_first_participation_amount_icp_e8s = 400_000 * E8;

    // The amount that user 2 will attempt to participate with. Even though this is greater than
    // the per-participant minimum, it won't work, because there is "not enough room" left in
    // the swap to accept this user's participation while also honoring the per-participant minimum.
    let user_2_participation_amount_icp_e8s = 150_000 * E8;

    // The amount that will be participated by user 1 at the end.
    let user_1_second_participation_amount_icp_e8s = 100_000 * E8;

    let max_direct_participation_icp_e8s = 500_000 * E8;
    let total_nf_maturity_equivalent_icp_e8s = 2_000_000 * E8;
    let max_neurons_fund_participation_icp_e8s = total_nf_maturity_equivalent_icp_e8s / 10;

    // Slightly more than `user_2_participation_amount_icp_e8s`, but less than `user_1_first_participation_amount_icp_e8s`.
    let min_participant_icp_e8s = 150_000 * E8;

    let params = Some(Params {
        min_direct_participation_icp_e8s: Some(250_000 * E8),
        max_direct_participation_icp_e8s: Some(500_000 * E8),
        min_participant_icp_e8s,
        max_participant_icp_e8s: 500_000 * E8,
        sns_token_e8s: 1_000_000 * E8,
        min_participants: 1,
        ..params()
    });

    let mut swap = {
        // The Neuron's Fund should not affect the possibility of swap finalization.
        let mut init = init_with_neurons_fund_funding();

        let neurons_fund_participation_constraints = Some(NeuronsFundParticipationConstraints {
            min_direct_participation_threshold_icp_e8s: Some(250_000 * E8),
            max_neurons_fund_participation_icp_e8s: Some(max_neurons_fund_participation_icp_e8s),
            // Set `slope_numerator` to zero, so the outcome does not depend on the kind of matching
            // function that is used. Only `intercept_icp_e8s` will have an impact on the amount
            // that the Neurons' Fund participates on each of the three intervals.
            coefficient_intervals: vec![LinearScalingCoefficient {
                from_direct_participation_icp_e8s: Some(0),
                to_direct_participation_icp_e8s: Some(u64::MAX),
                // Does not matter what we set hese fields to (as long as the payload validates),
                // as the function should never be applied with the below `Init`:
                // neurons_fund_participation: Some(false).
                slope_numerator: Some(123_456_678 * E8),
                slope_denominator: Some(123_456_678 * E8),
                intercept_icp_e8s: Some(123_456_678 * E8),
            }],
            ideal_matched_participation_function: Some(IdealMatchedParticipationFunction {
                serialized_representation: Some(
                    PolynomialMatchingFunction::new(
                        total_nf_maturity_equivalent_icp_e8s,
                        neurons_fund_participation_limits(),
                        false,
                    )
                    .unwrap()
                    .serialize(),
                ),
            }),
        });
        init = Init {
            neurons_fund_participation_constraints,
            neurons_fund_participation: Some(true),
            ..init
        };
        init.validate().unwrap();
        let swap = Swap::new(init);
        Swap {
            params,
            lifecycle: Open as i32,
            ..swap
        }
    };

    // Preconditions
    assert_eq!(swap.lifecycle(), Open);
    assert_eq!(
        swap.max_direct_participation_e8s(),
        max_direct_participation_icp_e8s
    );
    assert_eq!(swap.current_neurons_fund_participation_e8s(), 0);
    assert_eq!(swap.current_direct_participation_e8s(), 0);
    assert_eq!(swap.current_total_participation_e8s(), 0);
    assert_eq!(
        swap.available_direct_participation_e8s(),
        max_direct_participation_icp_e8s
    );

    // Operation A: User 1 participates with amount `user_1_first_participation_amount_icp_e8s`.
    assert_eq!(
        call_refresh_buyer_token_e8s(&mut swap, &user1, user_1_first_participation_amount_icp_e8s),
        Ok(RefreshBuyerTokensResponse {
            icp_accepted_participation_e8s: user_1_first_participation_amount_icp_e8s,
            icp_ledger_account_balance_e8s: user_1_first_participation_amount_icp_e8s,
        })
    );

    assert_eq!(swap.lifecycle(), Open);
    assert_eq!(
        swap.current_direct_participation_e8s(),
        user_1_first_participation_amount_icp_e8s
    );
    assert_eq!(
        swap.current_neurons_fund_participation_e8s(),
        max_neurons_fund_participation_icp_e8s
    );
    assert_eq!(
        swap.current_total_participation_e8s(),
        user_1_first_participation_amount_icp_e8s + max_neurons_fund_participation_icp_e8s
    );
    assert_eq!(
        swap.available_direct_participation_e8s(),
        max_direct_participation_icp_e8s - user_1_first_participation_amount_icp_e8s
    );

    // Operation B: User 2 attempts to participate with amount `user_2_participation_amount_icp_e8s`.
    assert_eq!(
        call_refresh_buyer_token_e8s(&mut swap, &user2, user_2_participation_amount_icp_e8s),
        Err(format!(
            "Rejecting participation of effective amount {}; minimum required to participate: {}",
            swap.available_direct_participation_e8s(),
            min_participant_icp_e8s
        ))
    );

    // Postcondition B: The state should not have changed, so we're still in the precondition state.
    assert_eq!(swap.lifecycle(), Open);
    assert_eq!(
        swap.current_direct_participation_e8s(),
        user_1_first_participation_amount_icp_e8s
    );
    assert_eq!(
        swap.current_neurons_fund_participation_e8s(),
        max_neurons_fund_participation_icp_e8s
    );
    assert_eq!(
```
