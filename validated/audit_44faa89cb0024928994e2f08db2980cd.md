Audit Report

## Title
SNS Swap New-Participant Blocking via Residual-Capacity Griefing — (File: `rs/sns/swap/src/swap.rs`)

## Summary
An unprivileged ICP holder can participate in an open SNS swap with a carefully chosen amount that leaves `available_direct_participation_e8s` strictly between zero and `min_participant_icp_e8s`. Once in this state, every subsequent `refresh_buyer_token_e8s` call by a new participant is rejected because the effective accepted amount is capped below the per-participant minimum. If `min_participants` has not yet been reached, the swap aborts at its deadline and all ICP — including the attacker's — is refunded, permanently cancelling the SNS launch at no net cost to the attacker.

## Finding Description
Inside `refresh_buyer_token_e8s` in `rs/sns/swap/src/swap.rs`:

**Step 1** — The remaining capacity is computed: [1](#0-0) 

**Step 2** — The actual increment is capped at that remaining capacity, and the new balance is derived: [2](#0-1) 

**Step 3** — The resulting balance is checked against the per-participant minimum: [3](#0-2) 

For a **new** participant, `old_amount_icp_e8s = 0`, so `new_balance_e8s = actual_increment_e8s = min(available, requested)`. If `available < min_participant_icp_e8s`, the check at line 1241 always fires regardless of how much ICP the new participant transfers.

The only pre-participation guard, `validate_possibility_of_direct_participation`, does **not** catch this state — it only rejects when the ICP target is already reached or exceeded, not when remaining capacity is below the per-participant minimum: [4](#0-3) 

`available_direct_participation_e8s` is simply `max − current` with no floor check: [5](#0-4) 

An attacker achieves the griefing state by participating with exactly `max_direct_participation_e8s − (min_participant_icp_e8s − 1)` ICP, leaving `min_participant_icp_e8s − 1` e8s of remaining capacity. The existing test `test_swap_cannot_finalize_via_new_participation_if_remaining_lt_minimal_participation_amount` explicitly confirms this rejection behavior: [6](#0-5) 

Existing participants are unaffected because their `old_amount_icp_e8s ≥ min_participant_icp_e8s`, keeping `new_balance_e8s` above the minimum even after the tiny increment is added, as confirmed by the test at lines 5874–5887: [7](#0-6) 

## Impact Explanation
This is a **High** severity finding. An unprivileged actor can permanently cancel an SNS launch by blocking new participants from joining before `min_participants` is satisfied. The swap will reach its deadline, `sufficient_participation()` will return false, `try_abort` will succeed, and all ICP is refunded. This constitutes a significant SNS governance security impact with concrete, irreversible harm to the SNS project and its prospective participants — matching the allowed impact class: *"Significant SNS security impact with concrete user or protocol harm."*

## Likelihood Explanation
The attack requires only:
- Holding enough ICP to fill the swap to within `min_participant_icp_e8s − 1` e8s of its cap.
- Calling `refresh_buyer_token_e8s` once before `min_participants` is reached.

No privileged access is required. The entry point is the public `refresh_buyer_tokens` ingress method. The attacker bears **no net financial cost** since ICP is refunded on abort; the only cost is opportunity cost of locking ICP for the swap duration. When `max_participant_icp_e8s ≈ max_direct_participation_e8s` (common for smaller SNS launches), a single actor can execute the attack alone. Otherwise, two colluding actors suffice.

## Recommendation
After capping `actual_increment_e8s` at `available_direct_participation_e8s`, enforce the following invariant: a participation should only be accepted if it either (a) fills the swap exactly to its cap, or (b) leaves at least `min_participant_icp_e8s` of remaining capacity for future participants.

Concretely, after computing `new_balance_e8s` and before the minimum-balance check, compute the post-participation remaining capacity. If it would fall strictly between zero and `min_participant_icp_e8s`, either:
1. **Accept the full remaining capacity** for this participant (waiving the minimum for the final slot), or
2. **Reject the participation** with a clear error explaining that the swap can no longer accept new participants (only existing ones may top up), and emit a canister log/metric so the SNS team can observe the condition.

## Proof of Concept
Given a swap configured with:
- `max_direct_participation_icp_e8s = 500_000 * E8`
- `min_participant_icp_e8s = 1 * E8`
- `max_participant_icp_e8s = 500_000 * E8`
- `min_participants = 3`

**Step 1 — Attacker participates:**
Attacker transfers `499_999 * E8 + (E8 - 1)` ICP and calls `refresh_buyer_token_e8s`. `available_direct_participation_e8s()` becomes `1` (one e8). Attacker is recorded as buyer #1.

**Step 2 — Legitimate user attempts to join:**
User transfers `1 * E8` ICP and calls `refresh_buyer_token_e8s`.
- `max_increment_e8s = 1`
- `actual_increment_e8s = 1`
- `new_balance_e8s = 1 < min_participant_icp_e8s (= E8)`
- **Rejected**: `"Rejecting participation of effective amount 1; minimum required to participate: 100000000"`

**Step 3 — Swap deadline passes:**
Only 1 of 3 required participants joined. `try_abort` succeeds. Attacker's ICP is refunded in full. SNS launch fails.

This is directly reproducible as a unit test following the pattern of `test_swap_cannot_finalize_via_new_participation_if_remaining_lt_minimal_participation_amount`: [8](#0-7)

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

**File:** rs/sns/swap/src/swap.rs (L522-535)
```rust
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

**File:** rs/sns/swap/src/swap.rs (L1223-1225)
```rust
        let requested_increment_e8s = e8s - old_amount_icp_e8s;
        let actual_increment_e8s = std::cmp::min(max_increment_e8s, requested_increment_e8s);
        let new_balance_e8s = old_amount_icp_e8s.saturating_add(actual_increment_e8s);
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

**File:** rs/sns/swap/tests/swap.rs (L5707-5756)
```rust
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

**File:** rs/sns/swap/tests/swap.rs (L5874-5887)
```rust
    // Operation C: User 1 increases their participation by `user_1_second_participation_amount_icp_e8s`.
    assert_eq!(
        call_refresh_buyer_token_e8s(
            &mut swap,
            &user1,
            user_1_first_participation_amount_icp_e8s + user_1_second_participation_amount_icp_e8s
        ),
        Ok(RefreshBuyerTokensResponse {
            icp_accepted_participation_e8s: user_1_first_participation_amount_icp_e8s
                + user_1_second_participation_amount_icp_e8s,
            icp_ledger_account_balance_e8s: user_1_first_participation_amount_icp_e8s
                + user_1_second_participation_amount_icp_e8s,
        })
    );
```
