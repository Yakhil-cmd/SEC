### Title
Ticket Minimum-Participation Guard Checked Against Uncapped Increment Instead of Actual Credited Increment - (File: rs/sns/swap/src/swap.rs)

### Summary
In `refresh_buyer_token_e8s`, the SNS Swap canister checks a buyer's open ticket amount against `requested_increment_e8s` (the raw ledger balance increase) rather than `actual_increment_e8s` (the increment actually credited, which is capped by remaining swap capacity). When the swap is nearly full, the ticket is silently consumed even though the buyer's participation is less than the ticket amount, locking excess ICP in the swap subaccount.

### Finding Description

In `rs/sns/swap/src/swap.rs`, `refresh_buyer_token_e8s` computes two distinct increment values: [1](#0-0) 

- `requested_increment_e8s = e8s - old_amount_icp_e8s` — the raw increase observed from the ICP ledger balance.
- `actual_increment_e8s = std::cmp::min(max_increment_e8s, requested_increment_e8s)` — the increment actually applied, capped by the remaining swap capacity (`max_increment_e8s`).

The `new_balance_e8s` is then further capped by `max_participant_icp_e8s`: [2](#0-1) 

After these caps, the ticket validation check reads: [3](#0-2) 

The check `amount_ticket > requested_increment_e8s` compares the ticket amount against the **uncapped** raw increment. Because `requested_increment_e8s >= actual_increment_e8s` always holds, the check can pass (ticket consumed and deleted) even when `amount_ticket > actual_increment_e8s`, i.e., when the actual credited participation is less than what the ticket specified.

The correct check should be `amount_ticket > actual_increment_e8s` (or equivalently `amount_ticket > new_balance_e8s - old_amount_icp_e8s`).

**Concrete scenario:**
1. Swap has 40 ICP of remaining capacity (`max_increment_e8s = 40`).
2. Buyer creates a ticket for 80 ICP (valid at creation time when capacity was ≥ 80).
3. Another participant fills 60 ICP of the swap between ticket creation and this call.
4. Buyer deposits 80 ICP to their swap subaccount; `refresh_buyer_token_e8s` is called.
5. `requested_increment_e8s = 80`, `actual_increment_e8s = min(40, 80) = 40`.
6. Check: `amount_ticket (80) > requested_increment_e8s (80)` → **false** → ticket deleted.
7. Buyer is credited with only 40 ICP participation; the remaining 40 ICP is stranded in the swap subaccount until the swap finalizes.

### Impact Explanation

The buyer's open ticket is consumed (deleted from `OPEN_TICKETS_MEMORY`) even though the actual participation recorded is less than the ticket amount. The buyer's excess ICP is locked in the swap subaccount until the swap commits or aborts. The buyer cannot create a new ticket for the same participation attempt (the ticket system prevents duplicate tickets), and the participation shortfall is not surfaced as an error. This constitutes a **governance accounting bug** reachable by any unprivileged direct participant in an SNS swap. [4](#0-3) 

### Likelihood Explanation

Medium. The condition requires: (a) the swap to be in the `Open` lifecycle, (b) the buyer to have an open ticket, and (c) the remaining swap capacity (`max_increment_e8s`) to be less than the buyer's deposited increment. Condition (c) is a normal race condition that occurs naturally as a swap fills up near its `max_direct_participation_icp_e8s` limit. The ticketing system is used by participants following the recommended payment flow. [5](#0-4) 

### Recommendation

Replace the check at line 1262 with a comparison against the actual credited increment:

```rust
// Before (wrong):
if amount_ticket > requested_increment_e8s {

// After (correct):
if amount_ticket > actual_increment_e8s {
```

This ensures the ticket is only consumed when the actual participation meets or exceeds the ticket's stated amount, and returns an error otherwise so the buyer can retry after conditions change.

### Proof of Concept

Entry path: any unprivileged principal calls `refresh_buyer_token_e8s` (exposed as `refresh_buyer_tokens` on the SNS Swap canister) after depositing ICP to their swap subaccount, while holding an open ticket whose `amount_icp_e8s` exceeds the remaining swap capacity. [6](#0-5) 

The function is `async` and awaits the ICP ledger balance query before performing the ticket check, meaning the swap capacity can change between ticket creation and the check — a realistic race condition in any active SNS swap near its participation ceiling. [7](#0-6)

### Citations

**File:** rs/sns/swap/src/swap.rs (L1134-1141)
```rust
    pub async fn refresh_buyer_token_e8s(
        &mut self,
        buyer: PrincipalId,
        confirmation_text: Option<String>,
        this_canister: CanisterId,
        icp_ledger: &dyn ICRC1Ledger,
    ) -> Result<RefreshBuyerTokensResponse, String> {
        use swap_participation::*;
```

**File:** rs/sns/swap/src/swap.rs (L1153-1163)
```rust
        let e8s = {
            let account = Account {
                owner: this_canister.get().0,
                subaccount: Some(principal_to_subaccount(&buyer)),
            };
            icp_ledger
                .account_balance(account)
                .await
                .map_err(|x| x.to_string())?
                .get_e8s()
        };
```

**File:** rs/sns/swap/src/swap.rs (L1177-1177)
```rust
        let max_increment_e8s = self.available_direct_participation_e8s();
```

**File:** rs/sns/swap/src/swap.rs (L1222-1225)
```rust
        // Subtraction safe because of the preceding if-statement.
        let requested_increment_e8s = e8s - old_amount_icp_e8s;
        let actual_increment_e8s = std::cmp::min(max_increment_e8s, requested_increment_e8s);
        let new_balance_e8s = old_amount_icp_e8s.saturating_add(actual_increment_e8s);
```

**File:** rs/sns/swap/src/swap.rs (L1236-1237)
```rust
        // Limit the participation based on the maximum per participant.
        let new_balance_e8s = std::cmp::min(new_balance_e8s, max_participant_icp_e8s);
```

**File:** rs/sns/swap/src/swap.rs (L1262-1267)
```rust
            if amount_ticket > requested_increment_e8s {
                return Err(format!(
                    "The available balance to be topped up ({requested_increment_e8s}) \
                    by the buyer is smaller than the amount requested ({amount_ticket})."
                ));
            }
```

**File:** rs/sns/swap/src/swap.rs (L1268-1271)
```rust
            // The requested balance in the ticket matches the balance to be topped up in the swap
            // --> Delete fully executed ticket, if it exists and proceed with the top up
            memory::OPEN_TICKETS_MEMORY.with(|m| m.borrow_mut().remove(&principal));
            // If there exists no ticket for the buyer, the payment flow will simply ignore the ticket
```
