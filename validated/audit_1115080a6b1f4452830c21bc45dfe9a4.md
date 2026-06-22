### Title
Existing SNS Swap Participant Incorrectly Blocked from Increasing Contribution at Maximum Capacity - (File: rs/sns/swap/src/swap.rs)

### Summary
In `refresh_buyer_token_e8s`, the maximum-participants capacity check does not verify whether the calling buyer is already in the `buyers` map before applying the `+1` guard. When the swap is exactly at capacity, an existing participant who transfers additional ICP and calls `refresh_buyer_token_e8s` is rejected with the error "existing participants may still increase their ICP participation amount" — a promise the code itself breaks.

### Finding Description
`refresh_buyer_token_e8s` enforces a hard cap on the number of direct participants to ensure enough SNS neurons can be minted for all buyers:

```rust
// rs/sns/swap/src/swap.rs  lines 1179-1198
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
        "The swap has reached the maximum number of direct participants \
         ({num_direct_participants}) and does not accept new participants; \
         existing participants may still increase their ICP participation amount. …",
    ));
}
```

`self.buyers.len()` counts **all** current buyers, including the caller if they are already a participant. The check then unconditionally adds `+1`, treating the existing buyer as a brand-new entrant. The actual membership check that distinguishes new from existing buyers only appears **later** in the function:

```rust
// rs/sns/swap/src/swap.rs  lines 1275-1283
let is_preexisting_buyer = self.buyers.contains_key(&buyer.to_string());
if !is_preexisting_buyer {
    insert_buyer_into_buyers_list_index(buyer)…;
}
```

Because the capacity guard fires before this check, when the swap is exactly at capacity an existing participant's top-up call is rejected, even though no new slot would actually be consumed. [1](#0-0) [2](#0-1) 

### Impact Explanation
- An existing participant who has already transferred additional ICP to their swap subaccount cannot have that ICP accepted by the swap canister when the swap is at maximum capacity.
- The ICP sits locked in the subaccount until the swap closes; the participant must call `error_refund_icp` to recover it, which is only available after the swap ends.
- The error message explicitly promises "existing participants may still increase their ICP participation amount," making this a broken protocol guarantee that can mislead users.
- In the worst case, if `max_increment_e8s` (available direct participation headroom) is also near zero, the participant permanently loses the opportunity to increase their stake in the swap.

**Impact: High** — loss of participation rights and temporary ICP lock-up for existing swap participants.

### Likelihood Explanation
Any SNS swap that reaches the maximum number of direct participants (derived from `MAX_NEURONS_FOR_DIRECT_PARTICIPANTS` divided by `neuron_basket_construction_parameters.count`) will trigger this bug for every existing participant who attempts a top-up. Popular SNS launches routinely fill their participant caps. The call path is fully unprivileged: any principal can invoke `refresh_buyer_token_e8s` on the swap canister.

**Likelihood: High** — the condition (swap at capacity + existing participant top-up) is a normal, expected scenario. [3](#0-2) 

### Recommendation
Move the `is_preexisting_buyer` check before the capacity guard and skip the guard for existing participants:

```rust
// Check that the maximum number of participants has not been reached yet.
{
    let is_preexisting_buyer = self.buyers.contains_key(&buyer.to_string());
    if !is_preexisting_buyer {
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
                "The swap has reached the maximum number of direct participants …",
            ));
        }
    }
}
```

The later `is_preexisting_buyer` variable at line 1275 can then reuse the result computed above. [4](#0-3) 

### Proof of Concept

1. Deploy an SNS swap with `neuron_basket_construction_parameters.count = 1` and `MAX_NEURONS_FOR_DIRECT_PARTICIPANTS = N`.
2. Fill the swap with exactly `N` distinct buyers, each contributing `min_participant_icp_e8s`.
3. As buyer #1 (already in `self.buyers`), transfer an additional `min_participant_icp_e8s` to the swap subaccount.
4. Call `refresh_buyer_token_e8s` as buyer #1.
5. **Observed**: the call returns `Err("The swap has reached the maximum number of direct participants (N) and does not accept new participants; existing participants may still increase their ICP participation amount.")`.
6. **Expected**: the call succeeds and buyer #1's balance is increased, because no new participant slot is consumed.

The ICP transferred in step 3 is now locked in the subaccount until the swap closes, at which point the buyer must call `error_refund_icp` to recover it. [5](#0-4)

### Citations

**File:** rs/sns/swap/src/swap.rs (L1179-1198)
```rust
        // Check that the maximum number of participants has not been reached yet.
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
        }
```

**File:** rs/sns/swap/src/swap.rs (L1275-1283)
```rust
        let is_preexisting_buyer = self.buyers.contains_key(&buyer.to_string());
        if !is_preexisting_buyer {
            insert_buyer_into_buyers_list_index(buyer)
                .map_err(|grow_failed| {
                    format!(
                        "Failed to add buyer {buyer} to state, the canister's stable memory could not grow: {grow_failed}"
                    )
                })?;
        }
```

**File:** rs/nervous_system/common/src/lib.rs (L1-5)
```rust
use by_address::ByAddress;
use core::{
    cmp::Reverse,
    fmt::Debug,
    ops::{Add, AddAssign, Div, Mul, Sub},
```
