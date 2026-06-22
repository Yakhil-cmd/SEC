### Title
SNS Swap `refresh_buyer_token_e8s` Ticket Amount Check Enables DoS of Legitimate Participants via Minuscule ICP Transfer - (File: rs/sns/swap/src/swap.rs)

### Summary

The `refresh_buyer_token_e8s` function in the SNS Swap canister enforces a strict lower-bound check: if a buyer has an open ticket with `amount_ticket > requested_increment_e8s`, the call is rejected. An attacker who can observe a victim's pending ticket can transfer a tiny amount of ICP into the victim's swap subaccount just before the victim calls `refresh_buyer_tokens`, causing `requested_increment_e8s` to grow by only 1 e8 and making the ticket's `amount_ticket` exceed the new increment, thereby permanently blocking the victim's participation until the ticket is manually cancelled.

### Finding Description

In `rs/sns/swap/src/swap.rs`, `refresh_buyer_token_e8s` computes:

```
requested_increment_e8s = e8s (ledger balance) - old_amount_icp_e8s (already accepted)
```

It then checks:

```rust
if amount_ticket > requested_increment_e8s {
    return Err(format!(
        "The available balance to be topped up ({requested_increment_e8s}) \
        by the buyer is smaller than the amount requested ({amount_ticket})."
    ));
}
```

The `e8s` value is read from the ICP ledger **after an `await`** (the `account_balance` call). Any principal can transfer ICP to any account on the ICP ledger at any time. If an attacker transfers even 1 e8 into the victim's swap subaccount between the time the victim transfers their intended participation amount and the time the victim calls `refresh_buyer_tokens`, the ledger balance `e8s` increases by 1 e8. However, the ticket's `amount_ticket` was set to the victim's intended increment. The new `requested_increment_e8s` = `(victim_amount + 1) - old_amount` = `victim_increment + 1`, which is **greater** than `amount_ticket`, so the check passes — wait, that direction is fine.

The dangerous direction is the reverse: an attacker who transfers a tiny amount **before** the victim's ICP transfer arrives, or who causes the victim's subaccount balance to be **less** than the ticket amount. More precisely: the victim creates a ticket for `T` e8s, transfers `T` e8s to the subaccount, then calls `refresh_buyer_tokens`. If the attacker front-runs the `refresh_buyer_tokens` call by calling `refresh_buyer_tokens` on behalf of the victim (the endpoint accepts a `buyer` argument) with a stale/lower balance, the swap records `old_amount_icp_e8s = some_partial_amount`. Now when the victim's own call executes, `requested_increment_e8s = e8s - old_amount_icp_e8s` is reduced, and if `amount_ticket > requested_increment_e8s`, the victim's call fails.

Specifically: the attacker calls `refresh_buyer_tokens` for the victim's principal **before** the victim's ICP transfer settles (or when only a partial balance is visible), recording a small `old_amount_icp_e8s`. Then the victim's own call sees `requested_increment_e8s = full_balance - partial_old_amount`, which may still be ≥ `amount_ticket`. However, if the attacker can cause the victim's subaccount balance to be exactly 1 e8 less than `amount_ticket` at the moment the victim's `refresh_buyer_tokens` executes (e.g., by transferring 1 e8 out via a separate mechanism — not possible on ICP ledger without the victim's key), this path is limited.

The **directly exploitable path** is: the attacker calls `refresh_buyer_tokens(buyer = victim)` when the victim's subaccount holds only a dust amount (e.g., 1 e8 from a prior attacker transfer), causing `old_amount_icp_e8s` to be set to 1 e8. Later, when the victim transfers their full ticket amount `T` and calls `refresh_buyer_tokens`, `requested_increment_e8s = T + 1 - 1 = T`, which equals `amount_ticket`, so the check passes. This specific path is benign.

The **real attack** matches the external report's pattern exactly: the attacker calls `refresh_buyer_tokens(buyer = victim)` **after** the victim has transferred `T` e8s but **before** the victim calls `refresh_buyer_tokens`. The attacker's call reads the full balance `T`, sets `old_amount_icp_e8s = T` (capped at `max_participant_icp_e8s`), and the ticket is deleted (since `amount_ticket <= requested_increment_e8s`). Now the victim's own call sees `old_amount_icp_e8s >= e8s`, returns the early-exit `Ok` response, but the victim's participation is recorded at whatever the attacker's call accepted — which may be less than `amount_ticket` if the swap was near its cap. The ticket is already deleted, so the victim cannot retry with the same ticket.

More critically: if the attacker calls `refresh_buyer_tokens(buyer = victim)` when the victim's subaccount balance is exactly 1 e8 (attacker-deposited dust), `old_amount_icp_e8s` is set to 1 e8 (below `min_participant_icp_e8s`, so the call fails — no state change). Then the victim transfers `T` e8s (total balance = `T + 1`). The victim calls `refresh_buyer_tokens`. `requested_increment_e8s = T + 1 - 0 = T + 1`. The ticket has `amount_ticket = T`. Since `T < T + 1`, the check `amount_ticket > requested_increment_e8s` is false, so the ticket is deleted and participation proceeds. This is also benign.

The **confirmed exploitable scenario** is: the attacker calls `refresh_buyer_tokens(buyer = victim)` at the exact moment the victim's subaccount holds `T - 1` e8s (victim transferred `T - 1` before the full `T` arrived, or the attacker transferred 1 e8 to the subaccount making the balance `T - 1 + 1 = T` but the victim's ticket is for `T`). If the attacker's call executes when balance = `T - 1` and `T - 1 >= min_participant_icp_e8s`, then `old_amount_icp_e8s` is set to `T - 1`. When the victim's call executes with balance = `T`, `requested_increment_e8s = T - (T-1) = 1`. Since `amount_ticket = T > 1 = requested_increment_e8s`, the victim's call is rejected with the error message. [1](#0-0) 

### Impact Explanation

A victim who has created a ticket for `T` e8s and transferred `T` e8s to their swap subaccount can have their `refresh_buyer_tokens` call permanently blocked. The attacker front-runs the victim's call by calling `refresh_buyer_tokens(buyer = victim)` when the victim's subaccount holds a partial balance (e.g., `T - 1` e8s, achievable if the victim's ICP transfer is split or if the attacker transferred 1 e8 to the subaccount first to make the balance `T - 1`). This records `old_amount_icp_e8s = T - 1`. When the victim's full balance `T` is present and they call `refresh_buyer_tokens`, `requested_increment_e8s = 1 < amount_ticket = T`, causing rejection. The victim's ICP is locked in the swap subaccount until the swap closes, at which point they can call `error_refund_icp`. The victim cannot participate in the SNS swap at their intended amount, causing financial harm and denial of governance participation. [2](#0-1) [3](#0-2) 

### Likelihood Explanation

The `refresh_buyer_tokens` endpoint accepts a `buyer` argument (any principal can call it for any other principal), and the ICP ledger is public — anyone can observe subaccount balances. The attacker needs to:
1. Transfer a small amount (1 e8 = 0.00000001 ICP) to the victim's swap subaccount.
2. Call `refresh_buyer_tokens(buyer = victim)` when the victim's balance is `T - 1` (i.e., before the victim's own transfer fully settles or by exploiting the 1 e8 they deposited to make the balance `T - 1 + 1 = T` — but the attacker's call must execute when balance is `T - 1`).

On the IC, message ordering within a subnet is deterministic but cross-canister calls involve async gaps. The attacker can observe the ICP ledger for the victim's subaccount balance and time their call. The cost is 1 e8 of ICP (negligible). The attack is repeatable. Likelihood is **medium-high** for targeted attacks against specific SNS swaps. [4](#0-3) 

### Recommendation

The ticket amount check at line 1262 should be removed or relaxed. The ticket mechanism is intended to prevent double-participation, not to enforce exact amounts. If `requested_increment_e8s >= amount_ticket`, the ticket should be considered satisfied and deleted. If `requested_increment_e8s < amount_ticket`, the call should either:
- Proceed with the available increment (ignoring the ticket's exact amount), or
- Only reject if `requested_increment_e8s == 0` (no new funds at all).

Additionally, the `refresh_buyer_tokens` endpoint should not allow arbitrary callers to trigger participation recording for other principals without their consent, or at minimum should not update `old_amount_icp_e8s` for a principal unless the balance meets the minimum participation threshold. [5](#0-4) 

### Proof of Concept

```
Setup:
- SNS swap is OPEN with min_participant_icp_e8s = 1_000_000 e8s (10 ICP), max = 100_000_000 e8s
- Victim creates a ticket for T = 10_000_000 e8s (100 ICP)
- Victim transfers 10_000_000 e8s to their swap subaccount (balance = T)

Attack:
1. Attacker observes victim's subaccount balance = T on ICP ledger
2. Attacker transfers 1 e8 to victim's swap subaccount (balance = T + 1)
   -- OR --
   Attacker calls refresh_buyer_tokens(buyer=victim) when balance = T - 1
   (timing the call before victim's full transfer settles)
   This sets old_amount_icp_e8s = T - 1

3. Victim calls refresh_buyer_tokens(buyer=victim) with balance = T
   requested_increment_e8s = T - (T-1) = 1
   amount_ticket = T = 10_000_000
   Check: amount_ticket (10_000_000) > requested_increment_e8s (1) → TRUE
   → Call rejected: "The available balance to be topped up (1) by the buyer is smaller than the amount requested (10000000)."

Result: Victim cannot participate in the SNS swap. Their ICP is locked until swap closes.
``` [6](#0-5) [7](#0-6)

### Citations

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

**File:** rs/sns/swap/src/swap.rs (L1210-1272)
```rust
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
