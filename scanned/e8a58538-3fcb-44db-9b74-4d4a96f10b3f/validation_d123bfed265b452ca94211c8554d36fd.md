### Title
Missing `max_amount` Slippage Guard in SNS Swap `refresh_buyer_tokens` Allows Third-Party Frontrunning to Force Excess ICP Commitment - (File: `rs/sns/swap/src/swap.rs`)

---

### Summary

The SNS swap canister's `refresh_buyer_token_e8s` function reads the **full live balance** of a buyer's ICP subaccount from the ledger and commits that entire amount (up to `max_participant_icp_e8s`) as the buyer's participation. Because `RefreshBuyerTokensRequest` contains no `max_amount_icp_e8s` field, and because any principal can transfer ICP into a victim's swap subaccount at any time, an attacker can inflate the subaccount balance between the buyer's own transfer and their `refresh_buyer_tokens` call, forcing the buyer to commit more ICP than intended with no recourse until the swap closes.

---

### Finding Description

The SNS swap participation flow works as follows:

1. A buyer transfers X ICP to the swap canister's subaccount keyed by their principal (`principal_to_subaccount(&buyer)`).
2. The buyer calls `refresh_buyer_tokens`, which triggers `refresh_buyer_token_e8s`.
3. The function queries the **current live balance** of that subaccount from the ICP ledger.
4. It sets the buyer's registered participation to `min(live_balance, max_participant_icp_e8s)`.

The `RefreshBuyerTokensRequest` message carries only two fields — `buyer` (string) and `confirmation_text` (optional string) — with no field for the buyer to declare a maximum acceptable spend: [1](#0-0) 

The canister endpoint passes this directly to the core logic with no additional cap: [2](#0-1) 

Inside `refresh_buyer_token_e8s`, the live ledger balance is fetched unconditionally: [3](#0-2) 

That balance is then used to compute the new participation amount with no buyer-supplied upper bound: [4](#0-3) 

The ticket system (lines 1248–1272) is the only partial guard, but it is explicitly optional — the code comment states *"If there exists no ticket for the buyer, the payment flow will simply ignore the ticket"* — and it only checks that the ticket amount is not larger than the increment, not that the total balance matches the buyer's intent: [5](#0-4) 

Additionally, `refresh_buyer_tokens` accepts an arbitrary `buyer` string and can be called by **any** principal on behalf of any buyer, meaning the attacker can also trigger the registration step themselves after inflating the subaccount: [6](#0-5) 

---

### Impact Explanation

A buyer who intends to commit X ICP can be forced to commit up to `max_participant_icp_e8s` ICP. Once `refresh_buyer_tokens` succeeds, the buyer's participation is locked in the swap state. The ICP cannot be recovered until the swap reaches `COMMITTED` or `ABORTED` state, at which point only the **excess** above the accepted amount is refundable via `error_refund_icp`. If the swap commits, the buyer receives SNS tokens proportional to their inflated commitment — a larger position than intended, with the corresponding ICP permanently transferred to SNS governance. The buyer has no mechanism to cap their spend at the time of participation. [7](#0-6) 

---

### Likelihood Explanation

The attack requires the attacker to transfer ICP to the victim's subaccount, which is a permanent loss of funds for the attacker (the excess is refunded to the **buyer**, not the attacker). This makes purely profit-motivated attacks unlikely. However, the attack is realistic in adversarial scenarios: a competing participant wishing to drain a rival's ICP liquidity, force a larger-than-intended SNS position on a target, or accelerate the swap's ICP ceiling to crowd out other participants. The subaccount address is fully deterministic and public (`principal_to_subaccount` of the buyer's principal against the swap canister ID), so no privileged information is required. The window of vulnerability is the entire `OPEN` lifecycle of the swap.

---

### Recommendation

Add an optional `max_amount_icp_e8s: opt nat64` field to `RefreshBuyerTokensRequest`. Inside `refresh_buyer_token_e8s`, after reading the live ledger balance, reject the call if the computed `new_balance_e8s` would exceed the caller-supplied maximum:

```rust
if let Some(max) = max_amount_icp_e8s {
    if new_balance_e8s > max {
        return Err(format!(
            "Participation amount {} exceeds caller-specified maximum {}",
            new_balance_e8s, max
        ));
    }
}
```

This is the direct analog to the `maxCash` parameter added to `DutchAuction.bid()` in the referenced report. The parameter should be documented as the maximum ICP the buyer consents to commit in this call. [1](#0-0) 

---

### Proof of Concept

**Setup:** SNS swap is in `OPEN` state. `max_participant_icp_e8s = 100 ICP`. Victim intends to participate with 10 ICP.

1. **Victim** transfers 10 ICP to `Account { owner: swap_canister_id, subaccount: Some(principal_to_subaccount(&victim_principal)) }` on the ICP ledger.

2. **Attacker** (any unprivileged principal) transfers 90 ICP to the same subaccount address (publicly computable from the victim's principal and the swap canister ID).

3. **Attacker** (or victim) calls:
   ```
   refresh_buyer_tokens(RefreshBuyerTokensRequest {
       buyer: victim_principal.to_string(),
       confirmation_text: None,
   })
   ```

4. Inside `refresh_buyer_token_e8s`:
   - `e8s` = 100 ICP (live balance = 10 + 90)
   - `max_participant_icp_e8s` = 100 ICP
   - `new_balance_e8s` = min(100, 100) = 100 ICP
   - Victim's `BuyerState` is set to 100 ICP

5. **Result:** Victim is committed to 100 ICP. The 90 ICP the attacker transferred is permanently swept to SNS governance on finalization. The attacker loses 90 ICP; the victim receives 10× more SNS tokens than intended and has 90 ICP permanently removed from their control until swap close. [8](#0-7) [4](#0-3)

### Citations

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L843-855)
```text
message RefreshBuyerTokensRequest {
  // If not specified, the caller is used.
  string buyer = 1;

  // To accept the swap participation confirmation, a participant should send
  // the confirmation text via refresh_buyer_tokens, matching the text set
  // during SNS initialization.
  optional string confirmation_text = 2;
}
message RefreshBuyerTokensResponse {
  uint64 icp_accepted_participation_e8s = 1;
  uint64 icp_ledger_account_balance_e8s = 2;
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

**File:** rs/sns/swap/src/swap.rs (L1134-1163)
```rust
    pub async fn refresh_buyer_token_e8s(
        &mut self,
        buyer: PrincipalId,
        confirmation_text: Option<String>,
        this_canister: CanisterId,
        icp_ledger: &dyn ICRC1Ledger,
    ) -> Result<RefreshBuyerTokensResponse, String> {
        use swap_participation::*;

        // These two checks need to be repeated after awaiting the response from the ICP ledger.
        self.validate_lifecycle_is_open()
            .map_err(context_before_awaiting_icp_ledger_response)?;
        self.validate_possibility_of_direct_participation()
            .map_err(context_before_awaiting_icp_ledger_response)?;

        // User input validation doesn't expire after await, so this check doesn't need repetition.
        self.validate_confirmation_text(confirmation_text)?;

        // Look for the token balance of the specified principal's subaccount on 'this' canister.
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

**File:** rs/sns/swap/src/swap.rs (L1208-1237)
```rust
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
```

**File:** rs/sns/swap/src/swap.rs (L1248-1272)
```rust
        // Try to fetch the current ticket of the buyer
        let principal = Blob::from_bytes(buyer.as_slice().into());
        if let Some(ticket_sns_sale_canister) =
            memory::OPEN_TICKETS_MEMORY.with(|m| m.borrow().get(&principal))
        {
            let amount_ticket = ticket_sns_sale_canister.amount_icp_e8s;
            // If the user has already bought tokens in this swap at a prior to the current purchase the
            // balance in the subaccount of the SNS sales canister that corresponds to the user will
            // show both the ICP balance used for the previous buy and the ICP balance used to make
            // this new purchase of SNS tokens (requested_increment_e8s + old_amount_icp_e8s).
            // If the ticket has a lower amount specified than what is the requested amount of
            // tokens according to the ICP balance in the subaccount, this check should pass
            // and the actual requested amount of tokens will be used.
            // Lower amounts than specified on the ticket are not excepted.
            if amount_ticket > requested_increment_e8s {
                return Err(format!(
                    "The available balance to be topped up ({requested_increment_e8s}) \
                    by the buyer is smaller than the amount requested ({amount_ticket})."
                ));
            }
            // The requested balance in the ticket matches the balance to be topped up in the swap
            // --> Delete fully executed ticket, if it exists and proceed with the top up
            memory::OPEN_TICKETS_MEMORY.with(|m| m.borrow_mut().remove(&principal));
            // If there exists no ticket for the buyer, the payment flow will simply ignore the ticket
        }
```

**File:** rs/sns/swap/src/swap.rs (L1285-1291)
```rust
        self.buyers
            .entry(buyer.to_string())
            .or_insert_with(|| BuyerState::new(0))
            .set_amount_icp_e8s(new_balance_e8s);
        // We compute the current participation amounts once and store the result in Swap's state,
        // for efficiency reasons.
        self.update_total_participation_amounts();
```
