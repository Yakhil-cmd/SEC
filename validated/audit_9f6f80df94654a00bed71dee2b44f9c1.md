Based on my code review, the claim is confirmed by the actual source.

**Key findings:**

1. The guard at [1](#0-0)  applies unconditionally to all callers — there is no `self.buyers.contains_key(&buyer.to_string())` check before the neuron-basket ceiling test.

2. `self.buyers.len()` already includes existing buyers, so `(num_direct_participants + 1)` over-counts when an existing buyer calls the function. [2](#0-1) 

3. The error message at lines 1191-1195 explicitly promises "existing participants may still increase their ICP participation amount" — a guarantee the code does not honour. [3](#0-2) 

4. The existing test `test_refresh_buyer_tokens_no_sns_neuron_baskets_available` only verifies that a new user (user1) is blocked and that user2's *current* balance is unchanged via `check_final_conditions`; it never attempts a top-up call from an existing participant after the limit is reached. [4](#0-3) 

---

Audit Report

## Title
`refresh_buyer_token_e8s` Incorrectly Blocks Existing Participants from Topping Up When Neuron-Basket Limit Is Reached — (File: `rs/sns/swap/src/swap.rs`)

## Summary
The neuron-basket ceiling guard in `refresh_buyer_token_e8s` applies to all callers without first checking whether the caller is already a recorded buyer. Once `(buyers.len() + 1) * basket_count > MAX_NEURONS_FOR_DIRECT_PARTICIPANTS`, every subsequent call — including top-ups from existing participants — is rejected with an error message that explicitly promises the opposite behaviour. ICP transferred to the swap subaccount for a top-up is then locked until the swap closes.

## Finding Description
In `rs/sns/swap/src/swap.rs` at lines 1180–1197, the guard computes `num_direct_participants = self.buyers.len()`, which already includes every existing buyer. The condition `(num_direct_participants + 1) * num_sns_neurons_per_basket > MAX_NEURONS_FOR_DIRECT_PARTICIPANTS` is then evaluated for every caller with no prior check of `self.buyers.contains_key(&buyer.to_string())`. An existing buyer calling `refresh_buyer_token_e8s` does not add a new entry to `self.buyers` (as shown at lines 1274–1288 where `is_preexisting_buyer` is checked only for the stable-memory index, not for the ceiling guard), yet the ceiling guard treats them as a prospective new participant. The `is_preexisting_buyer` variable computed later in the function at line 1275 is never fed back to gate the earlier guard. The error string at lines 1191–1195 states "existing participants may still increase their ICP participation amount," directly contradicting the code path that rejects them.

## Impact Explanation
Fits the allowed High impact class: "Significant SNS security impact with concrete user or protocol harm." Existing participants who transfer additional ICP to their swap subaccount expecting a top-up find the call rejected; that ICP is locked in the subaccount until the swap closes and `error_refund_icp` becomes callable. A malicious actor controlling enough principals (each funding the minimum participation amount) can deliberately fill the remaining participant slots, permanently blocking all existing participants from increasing their contribution for the remainder of the swap's open window. The swap's documented invariant — that existing participants retain top-up rights — is violated.

## Likelihood Explanation
Reachable by any unprivileged user. No governance majority, subnet majority, or privileged access is required. The public endpoint `refresh_buyer_tokens` in `canister/canister.rs` (lines 128–143) accepts an arbitrary `buyer` principal. With `neuron_basket_count = 33_000` (as used in the existing test), the ceiling is hit with only three participants. An attacker needs only to register enough principals with the minimum ICP amount to push `(buyers.len() + 1) * basket_count` over `MAX_NEURONS_FOR_DIRECT_PARTICIPANTS`. The attack is repeatable across any SNS swap that configures a sufficiently large basket count.

## Recommendation
Before the neuron-basket ceiling check, determine whether the caller is already a participant and skip the guard for existing buyers:

```rust
let is_new_buyer = !self.buyers.contains_key(&buyer.to_string());
if is_new_buyer
    && (num_direct_participants + 1) * num_sns_neurons_per_basket
        > MAX_NEURONS_FOR_DIRECT_PARTICIPANTS
{
    return Err(format!("…"));
}
```

Additionally, add a test that calls `buy_token_ok` from an existing participant after the ceiling is reached to prevent regression.

## Proof of Concept
1. Deploy a swap with `neuron_basket_count = 33_000` and `max_direct_participation_icp_e8s = 100 * E8`.
2. Have `user2`, `user3`, `user4` each participate; after three participants `(3+1)*33_000 > MAX_NEURONS_FOR_DIRECT_PARTICIPANTS`.
3. Transfer additional ICP to `user2`'s swap subaccount.
4. Call `refresh_buyer_tokens` as `user2` (already in `self.buyers`).
5. Observe the call returns `"The swap has reached the maximum number of direct participants … existing participants may still increase their ICP participation amount."` — the top-up is rejected despite the error message's promise, and `user2`'s additional ICP is locked until the swap closes.

This is directly reproducible by extending `test_refresh_buyer_tokens_no_sns_neuron_baskets_available` with a `buy_token_ok` call for `user2` after the three participants have been registered.

### Citations

**File:** rs/sns/swap/src/swap.rs (L1180-1197)
```rust
        {
            let num_direct_participants = self.buyers.len() as u64;
            let num_sns_neurons_per_basket = params
                .neuron_basket_construction_parameters
                .as_ref()
                .expect("neuron_basket_construction_parameters must be specified")
                .count;
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

**File:** rs/sns/swap/tests/swap.rs (L5030-5044)
```rust
    // No user that has not participated in the swap yet can buy this one token left
    buy_token_err(
        &mut swap,
        &user1,
        &amount_user1_0,
        "The swap has reached the maximum number of direct participants",
    );

    // The one token should still be left fur purchase
    check_final_conditions(
        &mut swap,
        &user2,
        &amount_user2_0,
        &(params.max_direct_participation_icp_e8s.unwrap() - E8),
    );
```
