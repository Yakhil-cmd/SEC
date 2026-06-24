### Title
SNS Swap `refresh_buyer_token_e8s()` Can Be Griefed by Existing Participants to Lock Victim ICP and Block New Participation - (File: rs/sns/swap/src/swap.rs)

---

### Summary

An existing participant in an SNS decentralization swap can deliberately top up their participation by a small amount to push the remaining available ICP capacity below `min_participant_icp_e8s`. This causes any new participant's `refresh_buyer_token_e8s` call to hard-fail even after the victim has already transferred ICP to the swap canister's subaccount, locking those funds until the swap closes. The same root cause also blocks the `new_sale_ticket` ticket-creation path for new participants.

---

### Finding Description

In `refresh_buyer_token_e8s`, the function correctly caps the accepted increment to the available capacity:

```rust
let actual_increment_e8s = std::cmp::min(max_increment_e8s, requested_increment_e8s);
let new_balance_e8s = old_amount_icp_e8s.saturating_add(actual_increment_e8s);
``` [1](#0-0) 

However, immediately after capping, the function performs a hard rejection if the resulting `new_balance_e8s` falls below `min_participant_icp_e8s`:

```rust
if new_balance_e8s < params.min_participant_icp_e8s {
    return Err(format!(
        "Rejecting participation of effective amount {}; minimum required to participate: {}",
        new_balance_e8s, params.min_participant_icp_e8s
    ));
}
``` [2](#0-1) 

Because `new_balance_e8s` is derived from `max_increment_e8s` (the remaining capacity), any existing participant who tops up to leave `available_direct_participation_e8s < min_participant_icp_e8s` will cause every subsequent new-participant call to hit this hard error — even if the victim already transferred ICP to the swap canister's subaccount.

The identical root cause exists in `new_sale_ticket` via `compute_participation_increment`:

```rust
// We do not want users to participate less than min_user_participation
// even if that's what's remaining in the swap.
if user_participation.saturating_add(max_available_increment) < min_user_participation {
    return Err((0, 0));
}
``` [3](#0-2) 

This is called from `new_sale_ticket` and returns `err_invalid_user_amount(0, 0)` to the caller: [4](#0-3) 

The asymmetry is critical: existing participants are **not** blocked by this check because their `user_participation > 0` means `user_participation + max_available_increment` can still satisfy `min_user_participation`, while a new participant with `user_participation = 0` is unconditionally rejected whenever `max_available_increment < min_user_participation`.

---

### Impact Explanation

1. **ICP locked in swap subaccount.** A victim who transferred ICP to the swap canister's subaccount before calling `refresh_buyer_token_e8s` cannot reclaim those funds until the swap closes (committed or aborted) and `error_refund_icp` becomes callable. The comment in the code itself acknowledges this risk: *"If a ledger transfer was successfully made, but this call fails (many reasons are possible), the owner of the ICP sent to the subaccount can reclaim their tokens using `error_refund_icp` once this swap is closed."* [5](#0-4) 

2. **Reduced decentralization.** Blocking new participants reduces the number of unique token holders, directly undermining the decentralization goal of the SNS swap.

3. **Capacity monopolization.** After pushing remaining capacity below `min_participant_icp_e8s`, the attacker (an existing participant) can still consume the residual capacity themselves, since their `user_participation + max_available_increment >= min_user_participation` still holds.

---

### Likelihood Explanation

Moderate. The attacker must:
- Already be a registered participant in the swap (low barrier — anyone can join early).
- Have headroom below `max_participant_icp_e8s` to top up (common in practice).
- Submit a top-up message that leaves exactly `available < min_participant_icp_e8s` remaining.

No privileged role, admin key, or subnet-majority corruption is required. The IC's deterministic per-subnet message ordering means the attacker can reliably sequence their top-up before a victim's `refresh_buyer_token_e8s` call by submitting first. The swap state is publicly queryable, so the attacker can observe the exact remaining capacity before acting.

---

### Recommendation

Mirror the LPDA.sol recommended fix: instead of hard-rejecting when the capped balance falls below the minimum, waive the minimum enforcement when the remaining capacity itself is already below `min_participant_icp_e8s`. This ensures the victim can absorb whatever capacity remains rather than being locked out entirely.

```rust
// Current (vulnerable):
if new_balance_e8s < params.min_participant_icp_e8s {
    return Err(format!(...));
}

// Recommended:
// Only enforce the minimum when there is enough remaining capacity
// to satisfy it. If remaining capacity is already below the minimum,
// accept the partial increment so the victim is not locked out.
if new_balance_e8s < params.min_participant_icp_e8s
    && max_increment_e8s >= params.min_participant_icp_e8s
{
    return Err(format!(
        "Rejecting participation of effective amount {}; minimum required to participate: {}",
        new_balance_e8s, params.min_participant_icp_e8s
    ));
}
```

Apply the same fix to `compute_participation_increment` (used by `new_sale_ticket`):

```rust
// Current (vulnerable):
if user_participation.saturating_add(max_available_increment) < min_user_participation {
    return Err((0, 0));
}

// Recommended: only block new participants when the shortfall is
// not caused by the global cap being already below the minimum.
if max_available_increment < min_user_participation
    && user_participation == 0
    && user_participation.saturating_add(max_available_increment) < min_user_participation
{
    // Accept the remaining capacity rather than rejecting outright.
}
```

---

### Proof of Concept

**Setup:** `max_direct_participation_icp_e8s = 100 ICP`, `min_participant_icp_e8s = 2 ICP`, `max_participant_icp_e8s = 40 ICP`.

1. Bob participates early with 38 ICP. Total = 38 ICP, remaining = 62 ICP.
2. Other participants fill the swap to 98 ICP. Remaining = 2 ICP.
3. Alice (new participant) transfers 5 ICP to the swap canister's subaccount for her principal.
4. **Bob tops up by 1 ICP** (total = 99 ICP, remaining = 1 ICP). Bob's call succeeds because `38 + 1 + 1 = 40 >= min_participant_icp_e8s`.
5. Alice calls `refresh_buyer_token_e8s`:
   - `e8s = 5 ICP` — passes the raw-balance check at line 1202 (`5 >= 2`).
   - `max_increment_e8s = 1 ICP` (remaining capacity).
   - `actual_increment_e8s = min(1, 5) = 1 ICP`.
   - `new_balance_e8s = 0 + 1 = 1 ICP`.
   - **Hard error at line 1241**: `1 < min_participant_icp_e8s (2)` → rejected.
6. Alice's 5 ICP is locked in the swap subaccount until the swap closes.
7. Bob tops up by 1 more ICP, consuming the last remaining capacity. [6](#0-5) [7](#0-6)

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

**File:** rs/sns/swap/src/swap.rs (L2563-2574)
```rust
        let amount_icp_e8s = match compute_participation_increment(
            self.current_direct_participation_e8s(),
            params.max_direct_participation_icp_e8s.expect(
                "`params.max_direct_participation_icp_e8s` should always be set during Swap's initialization",
            ),
            params.min_participant_icp_e8s,
            params.max_participant_icp_e8s,
            old_balance_e8s,
            request.amount_icp_e8s,
        ) {
            Ok(amount_icp_e8s) => amount_icp_e8s,
            Err((min, max)) => return NewSaleTicketResponse::err_invalid_user_amount(min, max),
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
