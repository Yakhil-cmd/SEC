### Title
SNS Swap `refresh_buyer_token_e8s` Rejects Valid Deposits When Internally-Capped Amount Falls Below Minimum — (`rs/sns/swap/src/swap.rs`)

---

### Summary

The SNS swap canister's `refresh_buyer_token_e8s` function internally caps the user's effective participation amount by the remaining swap capacity (`available_direct_participation_e8s()`). When this cap reduces the effective amount below `min_participant_icp_e8s`, the function rejects the call — even though the user deposited a valid amount. The user's ICP is already locked in the swap canister's subaccount and cannot be recovered until the swap ends.

---

### Finding Description

In `rs/sns/swap/src/swap.rs`, `refresh_buyer_token_e8s` computes the effective participation amount through a chain of internal calculations:

1. It reads the user's ICP balance from the ledger (the user has already deposited ICP to their subaccount on the swap canister).
2. It caps the increment by `max_increment_e8s = self.available_direct_participation_e8s()` — the remaining capacity in the swap.
3. It caps again by `max_participant_icp_e8s`.
4. It then checks whether the resulting `new_balance_e8s` meets `min_participant_icp_e8s`. [1](#0-0) 

The critical sequence:

```rust
let actual_increment_e8s = std::cmp::min(max_increment_e8s, requested_increment_e8s);
let new_balance_e8s = old_amount_icp_e8s.saturating_add(actual_increment_e8s);
// ...
let new_balance_e8s = std::cmp::min(new_balance_e8s, max_participant_icp_e8s);

if new_balance_e8s < params.min_participant_icp_e8s {
    return Err(format!(
        "Rejecting participation of effective amount {}; minimum required to participate: {}",
        new_balance_e8s, params.min_participant_icp_e8s
    ));
}
``` [2](#0-1) 

**Concrete scenario:**
- `min_participant_icp_e8s = 5 * E8` (5 ICP)
- Swap has only `1 * E8` (1 ICP) of capacity remaining (`available_direct_participation_e8s()` returns 1 ICP)
- User deposits 5 ICP to their subaccount (a valid amount ≥ minimum)
- `actual_increment_e8s = min(1 ICP, 5 ICP) = 1 ICP`
- `new_balance_e8s = 0 + 1 ICP = 1 ICP`
- `1 ICP < 5 ICP` → **rejection**

The user deposited the correct amount, but the internally-computed effective amount is below the minimum. The ICP is already in the swap canister's subaccount and is locked until the swap concludes (commit or abort), at which point the user must claim a refund.

The `available_direct_participation_e8s()` value is not user-controlled — it depends on how much other participants have contributed, which can change between when the user queries the swap state and when their `refresh_buyer_token_e8s` call is processed. [3](#0-2) 

---

### Impact Explanation

- A user who deposits a valid amount (≥ `min_participant_icp_e8s`) receives a confusing rejection with the message "Rejecting participation of effective amount X; minimum required to participate: Y" — where Y is the minimum they already satisfied.
- Their ICP is locked in the swap canister's subaccount for the duration of the swap (up to 14 days per `MAX_SALE_DURATION_SECONDS`).
- They cannot participate in the swap despite having deposited the correct amount.
- The error message does not explain that the swap is nearly full; the user may believe they made an error. [4](#0-3) 

---

### Likelihood Explanation

This condition occurs whenever the swap's remaining direct participation capacity drops below `min_participant_icp_e8s`. This is a normal end-of-swap condition for any popular SNS launch. Any user who deposits ICP during this window — a window that can last for the entire remaining swap duration — is affected. The condition is reachable by any unprivileged caller of `refresh_buyer_token_e8s`. [5](#0-4) 

---

### Recommendation

When `max_increment_e8s` would reduce the effective participation below `min_participant_icp_e8s`, the function should either:

1. **Accept the capped amount** (skip the minimum check when the cap is due to swap capacity, not user input), or
2. **Return a distinct, informative error** (e.g., `SwapAlmostFull`) that distinguishes "your deposit is too small" from "the swap has insufficient remaining capacity to accept your full deposit at the minimum threshold."

The minimum check at line 1241 should only apply to the user's raw deposited amount (`e8s`), not to the internally-capped `new_balance_e8s`.

---

### Proof of Concept

```
1. Deploy SNS swap with min_participant_icp_e8s = 5 * E8, max_direct_participation = 100 * E8.
2. Fill the swap to 99 * E8 (leaving 1 * E8 capacity).
3. User deposits 5 * E8 ICP to their subaccount on the swap canister.
4. User calls refresh_buyer_token_e8s.
5. max_increment_e8s = 1 * E8 (remaining capacity).
6. actual_increment_e8s = min(1 * E8, 5 * E8) = 1 * E8.
7. new_balance_e8s = 1 * E8 < 5 * E8 = min_participant_icp_e8s.
8. Call returns Err("Rejecting participation of effective amount 100000000; minimum required to participate: 500000000").
9. User's 5 ICP is locked in the swap canister subaccount until swap ends.
``` [2](#0-1) [6](#0-5)

### Citations

**File:** rs/sns/swap/src/swap.rs (L1177-1178)
```rust
        let max_increment_e8s = self.available_direct_participation_e8s();

```

**File:** rs/sns/swap/src/swap.rs (L1200-1207)
```rust
        // Check that the minimum amount has been transferred before
        // actually creating an entry for the buyer.
        if e8s < params.min_participant_icp_e8s {
            return Err(format!(
                "Amount transferred: {}; minimum required to participate: {}",
                e8s, params.min_participant_icp_e8s
            ));
        }
```

**File:** rs/sns/swap/src/swap.rs (L1222-1246)
```rust
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
