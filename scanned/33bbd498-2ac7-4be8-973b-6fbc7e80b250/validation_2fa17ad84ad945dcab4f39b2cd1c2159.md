### Title
SNS Swap `refresh_buyer_token_e8s` Minimum Participation Check Permanently Locks Remaining Capacity When All Participants Are at Their Per-Participant Maximum - (`File: rs/sns/swap/src/swap.rs`)

---

### Summary

The SNS swap canister's `refresh_buyer_token_e8s` function enforces a `min_participant_icp_e8s` floor on every effective participation amount. When the remaining direct-participation capacity (`available_direct_participation_e8s()`) falls below `min_participant_icp_e8s` and every existing participant has already reached `max_participant_icp_e8s`, no one can fill the gap. The remaining ICP capacity is permanently locked, and the swap can never reach `max_direct_participation_icp_e8s`.

---

### Finding Description

`refresh_buyer_token_e8s` in `rs/sns/swap/src/swap.rs` contains two minimum-participation guards:

**Guard 1** (line 1202–1206) — rejects the call if the raw ledger balance is below the minimum:
```rust
if e8s < params.min_participant_icp_e8s {
    return Err(format!(
        "Amount transferred: {}; minimum required to participate: {}",
        e8s, params.min_participant_icp_e8s
    ));
}
```

**Guard 2** (line 1241–1246) — rejects the call if the *effective* new balance (after capping at both the per-participant maximum and the remaining swap capacity) is below the minimum:
```rust
let actual_increment_e8s = std::cmp::min(max_increment_e8s, requested_increment_e8s); // line 1224
let new_balance_e8s = old_amount_icp_e8s.saturating_add(actual_increment_e8s);         // line 1225
let new_balance_e8s = std::cmp::min(new_balance_e8s, max_participant_icp_e8s);          // line 1237
if new_balance_e8s < params.min_participant_icp_e8s {                                   // line 1241
    return Err(format!(
        "Rejecting participation of effective amount {}; minimum required to participate: {}",
        new_balance_e8s, params.min_participant_icp_e8s
    ));
}
```

When `remaining = available_direct_participation_e8s() < min_participant_icp_e8s`:

- **New participant** (`old_amount = 0`): `actual_increment = remaining`, `new_balance = remaining < min_participant_icp_e8s` → rejected by Guard 2.
- **Existing participant at `max_participant_icp_e8s`**: `actual_increment = remaining`, `new_balance = max_participant_icp_e8s + remaining`, then capped back to `max_participant_icp_e8s` by line 1237 → `new_balance = old_amount` (no-op). The call succeeds but writes nothing new; the remaining capacity is never consumed.

The `validate_participation_constraints` in `rs/sns/init/src/lib.rs` validates that `max_participant_icp_e8s <= max_direct_participation_icp_e8s` (line 1603) and that `max_direct_participation_icp_e8s >= min_participants * min_participant_icp_e8s` (line 1618), but it does **not** require that `max_direct_participation_icp_e8s` is reachable without leaving a sub-minimum remainder. Specifically, there is no check that `max_direct_participation_icp_e8s mod max_participant_icp_e8s < min_participant_icp_e8s` is impossible.

The developers are aware that remaining < min can block new participants (the test `test_swap_cannot_finalize_via_new_participation_if_remaining_lt_minimal_participation_amount` at line 5707 documents this), but the test only covers the case where an existing participant below their max can fill the gap. It does not cover the case where all existing participants are already at `max_participant_icp_e8s`.

---

### Impact Explanation

When the gap scenario is triggered, `max_direct_participation_icp_e8s` is permanently unreachable. The swap may still commit at the deadline if `min_direct_participation_icp_e8s` was met, but the SNS project loses the ICP that would have been raised in the locked remainder. For swaps where the maximum target is critical to the project's tokenomics (token price, neuron basket sizing), this constitutes a material fundraising shortfall and a denial-of-service on the maximum raise goal.

---

### Likelihood Explanation

The scenario is reachable without any privileged access. Any unprivileged principal can call `refresh_buyer_tokens` (the public `#[update]` endpoint in `rs/sns/swap/canister/canister.rs` line 128). A malicious participant can deliberately participate with exactly `max_participant_icp_e8s` to push the remaining capacity below `min_participant_icp_e8s`. Even without malicious intent, this can occur naturally whenever `max_direct_participation_icp_e8s` is not an exact multiple of `max_participant_icp_e8s` and the last few participants each fill to their per-participant cap.

Concrete example:
- `max_direct_participation_icp_e8s` = 100 ICP
- `min_participant_icp_e8s` = 10 ICP
- `max_participant_icp_e8s` = 33 ICP
- User 1, 2, 3 each participate with 33 ICP → remaining = 1 ICP
- No new participant can join (1 < 10); all existing participants are at their max
- 1 ICP of capacity is permanently locked

---

### Recommendation

In `refresh_buyer_token_e8s`, before applying Guard 2, check whether the caller is an **existing** participant whose `new_balance_e8s` would exceed their current `old_amount_icp_e8s` by at least the remaining capacity. If `actual_increment_e8s == max_increment_e8s` (i.e., the participant is consuming all remaining capacity), the minimum-balance check should be waived, analogous to the fix recommended in the external report:

```rust
// Allow consuming the exact remaining capacity even if it is below min_participant_icp_e8s.
let is_filling_remaining_capacity = actual_increment_e8s == max_increment_e8s;
if new_balance_e8s < params.min_participant_icp_e8s && !is_filling_remaining_capacity {
    return Err(format!(
        "Rejecting participation of effective amount {}; minimum required to participate: {}",
        new_balance_e8s, params.min_participant_icp_e8s
    ));
}
```

Additionally, add a validation in `validate_participation_constraints` (`rs/sns/init/src/lib.rs`) to ensure that `max_direct_participation_icp_e8s` can always be fully reached, e.g., by requiring `max_participant_icp_e8s >= min_participant_icp_e8s` (already enforced) and that the remainder `max_direct_participation_icp_e8s % max_participant_icp_e8s` is either 0 or `>= min_participant_icp_e8s`.

---

### Proof of Concept

1. Deploy an SNS swap with:
   - `max_direct_participation_icp_e8s = 100 * E8`
   - `min_participant_icp_e8s = 10 * E8`
   - `max_participant_icp_e8s = 33 * E8`
   - `min_participants = 1`

2. Three principals each transfer 33 ICP to their swap subaccount and call `refresh_buyer_tokens`. Each call succeeds; each buyer is recorded at 33 ICP. `available_direct_participation_e8s()` = 1 ICP.

3. A fourth principal transfers 10 ICP (≥ `min_participant_icp_e8s`) and calls `refresh_buyer_tokens`. Guard 2 fires: `new_balance = 0 + min(1, 10) = 1 < 10` → rejected with `"Rejecting participation of effective amount 1; minimum required to participate: 10"`.

4. Any of the three existing participants transfers additional ICP and calls `refresh_buyer_tokens`. Guard 2 passes (`new_balance = 33 + 1 = 34 >= 10`), but line 1237 caps `new_balance` back to `max_participant_icp_e8s = 33`. The state write is a no-op; `available_direct_participation_e8s()` remains 1 ICP.

5. The swap expires with `current_direct_participation = 99 ICP`, never reaching `max_direct_participation_icp_e8s = 100 ICP`.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

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

**File:** rs/sns/init/src/lib.rs (L1596-1625)
```rust
        if max_participant_icp_e8s < min_participant_icp_e8s {
            return Err(format!(
                "Error: max_participant_icp_e8s ({max_participant_icp_e8s}) must be >= min_participant_icp_e8s ({min_participant_icp_e8s})"
            ));
        }

        // (4)
        if max_participant_icp_e8s > max_direct_participation_icp_e8s {
            return Err(format!(
                "Error: max_participant_icp_e8s ({max_participant_icp_e8s}) \
                 must be <= max_direct_participation_icp_e8s ({max_direct_participation_icp_e8s})"
            ));
        }

        // (5)
        if max_direct_participation_icp_e8s > MAX_DIRECT_ICP_CONTRIBUTION_TO_SWAP {
            return Err(format!(
                "Error: max_direct_participation_icp_e8s ({max_direct_participation_icp_e8s}) can be at most {MAX_DIRECT_ICP_CONTRIBUTION_TO_SWAP} ICP E8s"
            ));
        }

        // (6)
        if max_direct_participation_icp_e8s
            < min_participants.saturating_mul(min_participant_icp_e8s)
        {
            return Err(format!(
                "Error: max_direct_participation_icp_e8s ({max_direct_participation_icp_e8s}) \
                 must be >= min_participants ({min_participants}) * min_participant_icp_e8s ({min_participant_icp_e8s})"
            ));
        }
```

**File:** rs/sns/swap/tests/swap.rs (L5702-5707)
```rust
/// Test that the `refresh_buyer_token_e8s` call fails in the special case when the remaining direct
/// participation amount is less than the minimal participation amount. In this scenario, the swap
/// cannot be finalized early by a new participant, only by an existing participant increasing their
/// participation.
#[test]
fn test_swap_cannot_finalize_via_new_participation_if_remaining_lt_minimal_participation_amount() {
```
