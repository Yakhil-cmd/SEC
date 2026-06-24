### Title
Third-Party Forced Participation via `refresh_buyer_tokens` Allows Griefing in SNS Swap - (File: rs/sns/swap/canister/canister.rs)

### Summary
The SNS swap canister's `refresh_buyer_tokens` endpoint accepts an arbitrary `buyer` principal from any caller. Because anyone can also send ICP to any principal's deterministic subaccount on the swap canister, an unprivileged third party can force any victim principal into swap participation — or inflate an existing participant's committed ICP — without the victim's consent.

### Finding Description
The canister endpoint `refresh_buyer_tokens` in `rs/sns/swap/canister/canister.rs` resolves the buyer principal from the request payload rather than enforcing `caller == buyer`:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    PrincipalId::from_str(&arg.buyer).unwrap()   // ← any caller, any buyer
};
``` [1](#0-0) 

The underlying `refresh_buyer_token_e8s` in `rs/sns/swap/src/swap.rs` then reads the live ICP ledger balance of `swap_canister / principal_to_subaccount(buyer)` and unconditionally raises the buyer's recorded participation to that balance (capped only by `max_participant_icp_e8s` and the remaining swap room):

```rust
let account = Account {
    owner: this_canister.get().0,
    subaccount: Some(principal_to_subaccount(&buyer)),
};
let e8s = icp_ledger.account_balance(account).await ...;
``` [2](#0-1) 

```rust
self.buyers
    .entry(buyer.to_string())
    .or_insert_with(|| BuyerState::new(0))
    .set_amount_icp_e8s(new_balance_e8s);
``` [3](#0-2) 

Because the subaccount address is deterministic (`principal_to_subaccount` is a public hash), any party can compute it and transfer ICP there via the ICP ledger. The `confirmation_text` field does not mitigate this: the text is set at SNS initialization and is publicly visible, so an attacker can always supply it. The ticket system is also bypassed when the victim has no open ticket. [4](#0-3) 

### Impact Explanation
**Forced participation / ICP lock-up.** An attacker sends ICP to `swap_canister / principal_to_subaccount(victim)` and calls `refresh_buyer_tokens(buyer = victim)`. The victim's `BuyerState.icp.amount_e8s` is raised to the new balance. The victim's ICP is now locked in the swap until it commits or aborts — without the victim ever signing a transaction to the swap canister.

**Swap outcome manipulation.** By stuffing multiple victims' subaccounts the attacker can push `current_direct_participation_e8s` to `max_direct_participation_icp_e8s`, triggering early commitment of the swap before the intended deadline. This harms legitimate participants who planned to participate later at a potentially better price, and can be used to commit a swap that would otherwise have aborted.

**Griefing / economic DoS.** Repeated stuffing before each heartbeat window can keep a victim perpetually locked into swap participation across multiple swaps, consuming their ICP liquidity.

### Likelihood Explanation
The attack requires no privileged role: any ingress sender can call `refresh_buyer_tokens` with an arbitrary `buyer` string, and any ICP holder can transfer to the deterministic subaccount. The confirmation text (when present) is public. The only cost to the attacker is the ICP transferred, which is credited to the victim's participation and returned on abort — making the attack nearly free in abort scenarios and a one-time cost in commit scenarios. Likelihood is **medium-high** for targeted griefing and **medium** for swap manipulation.

### Recommendation
Restrict `refresh_buyer_tokens` so that the resolved buyer principal must equal the ingress caller:

```rust
// Only allow refreshing one's own participation
let p = caller_principal_id();
// Ignore or reject arg.buyer if it differs from caller
``` [1](#0-0) 

If third-party notification is intentionally supported (e.g., for automated bots), add an explicit allowlist or require the caller to prove ownership of the ICP transfer (e.g., by signing the subaccount transfer).

### Proof of Concept
1. SNS swap is in `Open` lifecycle. Victim `Alice` has not participated.
2. Attacker computes `alice_subaccount = principal_to_subaccount(alice_principal)`.
3. Attacker calls ICP ledger `transfer` sending `X` ICP to `Account { owner: swap_canister_id, subaccount: alice_subaccount }`.
4. Attacker calls `refresh_buyer_tokens(RefreshBuyerTokensRequest { buyer: alice_principal.to_string(), confirmation_text: <public_text_or_None> })` from any identity.
5. `refresh_buyer_token_e8s` reads the balance (`X` ICP), finds `old_amount = 0`, sets `new_balance = min(X, max_participant_icp_e8s)`, and writes `BuyerState { icp: { amount_e8s: new_balance } }` for Alice.
6. Alice is now a registered participant with `new_balance` ICP locked. If the swap commits, Alice receives SNS tokens she never consented to purchase. If the swap aborts, Alice recovers the attacker's ICP — but the attacker can repeat the attack on the next swap.
7. By repeating step 3–5 across many victims, the attacker can push `current_direct_participation_e8s` to `max_direct_participation_icp_e8s`, forcing early commitment. [5](#0-4) [6](#0-5)

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
