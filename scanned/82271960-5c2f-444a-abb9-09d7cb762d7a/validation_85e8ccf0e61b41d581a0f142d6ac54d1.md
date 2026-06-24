### Title
Unprivileged Caller Can Trigger `refresh_buyer_tokens` for Any Buyer, Enabling Participation-Cap Front-Running in SNS Swap - (File: rs/sns/swap/canister/canister.rs)

### Summary
The SNS Swap canister's `refresh_buyer_tokens` update method accepts an arbitrary `buyer` principal from any unprivileged caller. Because the amount of ICP accepted for a buyer is bounded by the swap's remaining `available_direct_participation_e8s()` at execution time, an attacker who observes a victim's ICP transfer to the swap subaccount can race to consume the remaining cap — either by calling `refresh_buyer_tokens` for themselves or by triggering it for the victim at an inopportune moment — causing the victim to receive fewer SNS tokens than expected or to have their participation rejected outright. No minimum-SNS-tokens-received guarantee exists in the call interface.

### Finding Description
`refresh_buyer_tokens` in `rs/sns/swap/canister/canister.rs` resolves the buyer principal from the caller-supplied `arg.buyer` field without any authorization check:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    PrincipalId::from_str(&arg.buyer).unwrap()   // any caller, any buyer
};
``` [1](#0-0) 

The underlying `refresh_buyer_token_e8s` implementation reads the buyer's ICP ledger balance, then caps the accepted increment at `available_direct_participation_e8s()` — the remaining room under `max_direct_participation_icp_e8s`:

```rust
let max_increment_e8s = self.available_direct_participation_e8s();
...
let actual_increment_e8s = std::cmp::min(max_increment_e8s, requested_increment_e8s);
let new_balance_e8s = old_amount_icp_e8s.saturating_add(actual_increment_e8s);
``` [2](#0-1) 

`available_direct_participation_e8s()` is a live, shared counter that shrinks as other participants join:

```rust
pub fn available_direct_participation_e8s(&self) -> u64 {
    let max_direct_participation_e8s = self.max_direct_participation_e8s();
    let current_direct_participation_e8s = self.current_direct_participation_e8s();
    max_direct_participation_e8s
        .checked_sub(current_direct_participation_e8s)
        ...
}
``` [3](#0-2) 

The `RefreshBuyerTokensRequest` carries only `buyer` and `confirmation_text`; there is no `min_sns_tokens_out` or `min_icp_accepted` field that would let a buyer express a slippage bound. [4](#0-3) 

### Impact Explanation
When the swap is near its `max_direct_participation_icp_e8s` ceiling:

1. **Victim locks ICP**: Victim transfers ICP to their personal subaccount of the swap canister (a public, observable ICP ledger event).
2. **Attacker races**: Attacker calls `refresh_buyer_tokens` for themselves (or for the victim) before the victim's own call is processed, consuming the remaining cap.
3. **Victim is harmed**: When the victim's `refresh_buyer_tokens` call executes, `available_direct_participation_e8s()` is now smaller. The victim's accepted ICP is silently reduced to whatever remains, or the call fails entirely with "Rejecting participation of effective amount X; minimum required to participate: Y" if the remainder falls below `min_participant_icp_e8s`. [5](#0-4) 

The victim's excess ICP is stranded in the swap subaccount until the swap closes (committed or aborted), at which point it can be recovered only via `error_refund_icp`. The victim receives fewer SNS tokens than they intended to purchase, while the attacker secures a larger allocation. [6](#0-5) 

### Likelihood Explanation
The ICP ledger is fully public. Any observer can detect the moment a victim's ICP transfer to the swap subaccount is finalized and immediately submit a competing `refresh_buyer_tokens` call. IC consensus orders messages within a round non-deterministically from the perspective of external callers, so an attacker who submits their call in the same or an immediately following round has a realistic chance of being ordered first. The attack requires no privileged access, no key material, and no governance majority — only the ability to submit ingress messages, which any principal can do.

### Recommendation
1. **Add a `min_icp_accepted_e8s` parameter** to `RefreshBuyerTokensRequest` and reject the call if the actual accepted amount falls below the caller-specified floor. This is the direct analog of slippage protection.
2. **Restrict `refresh_buyer_tokens` to the buyer themselves** (i.e., require `arg.buyer == caller` or `arg.buyer.is_empty()`). Third-party triggering of another user's participation registration is the root enabler of the ordering attack.
3. Alternatively, implement a **commit-reveal or ticket-based scheme** so that a buyer's intended participation amount is recorded atomically with their ICP transfer, making the accepted amount independent of concurrent cap consumption.

### Proof of Concept
```
// Precondition: swap is open, available_direct_participation = 5 ICP,
//               min_participant_icp = 3 ICP, max_participant_icp = 10 ICP.

// Step 1: Victim transfers 4 ICP to swap subaccount (visible on ICP ledger).

// Step 2: Attacker (already a participant with 6 ICP) calls:
//   refresh_buyer_tokens({ buyer: attacker_principal, confirmation_text: None })
//   → attacker consumes remaining 4 ICP of cap (up to their per-participant max).
//   available_direct_participation is now 1 ICP.

// Step 3: Victim calls:
//   refresh_buyer_tokens({ buyer: "", confirmation_text: None })
//   → available = 1 ICP < min_participant_icp (3 ICP)
//   → Error: "Rejecting participation of effective amount 1 ICP;
//             minimum required to participate: 3 ICP"
//   Victim's 4 ICP is locked in the subaccount until swap closes.
``` [7](#0-6) [1](#0-0)

### Citations

**File:** rs/sns/swap/canister/canister.rs (L126-143)
```rust
/// See `Swap.refresh_buyer_token_e8`.
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

**File:** rs/sns/swap/src/swap.rs (L1127-1132)
```rust
    ///
    /// If a ledger transfer was successfully made, but this call
    /// fails (many reasons are possible), the owner of the ICP sent
    /// to the subaccount can reclaim their tokens using `error_refund_icp`
    /// once this swap is closed (committed or aborted).
    ///
```

**File:** rs/sns/swap/src/swap.rs (L1177-1225)
```rust
        let max_increment_e8s = self.available_direct_participation_e8s();

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
```

**File:** rs/sns/swap/src/swap.rs (L1239-1246)
```rust
        // Check that the new_balance_e8s is bigger than or equal to the minimum required for
        // participating.
        if new_balance_e8s < params.min_participant_icp_e8s {
            return Err(format!(
                "Rejecting participation of effective amount {}; minimum required to participate: {}",
                new_balance_e8s, params.min_participant_icp_e8s
            ));
        }
```
