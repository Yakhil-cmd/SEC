The code confirms the claim. Key verified facts:

- `canister.rs` L130-133: No `caller == buyer` check when `buyer` is non-empty. [1](#0-0) 
- `swap.rs` L1150: `validate_confirmation_text` validates the text but not who supplied it. [2](#0-1) 
- `swap.rs` L1248-1272: The ticket system is optional — if no ticket exists for the buyer, the flow proceeds without one. [3](#0-2) 
- `grep` for `caller.*buyer|buyer.*caller` across all swap Rust files: zero matches — no caller identity check exists anywhere in the path.

---

Audit Report

## Title
Unprivileged Caller Can Force SNS Swap Participation on Behalf of Any Buyer, Bypassing Confirmation Text Requirement - (File: `rs/sns/swap/canister/canister.rs`)

## Summary
The `refresh_buyer_tokens` update method accepts an arbitrary `buyer` principal in its request payload and performs no check that the caller matches the specified buyer. Any unprivileged ingress sender can call this method with a victim's principal as `buyer` and supply the publicly readable `confirmation_text`, forcing the victim's ICP — already deposited in the swap subaccount — into a committed swap participation without the victim's explicit consent. The confirmation text mechanism, designed to enforce informed consent, is completely bypassed.

## Finding Description
In `rs/sns/swap/canister/canister.rs` at L130-133, when `arg.buyer` is non-empty, it is parsed directly as a `PrincipalId` with no check that `caller == buyer`:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    PrincipalId::from_str(&arg.buyer).unwrap()  // attacker-controlled
};
```

This `p` is passed directly to `refresh_buyer_token_e8s` in `rs/sns/swap/src/swap.rs`. Inside that function, `validate_confirmation_text` (L1150) checks the supplied text against the SNS-configured text, but the SNS init payload — including `confirmation_text` — is publicly readable via the `get_init` query endpoint. There is no secret involved.

The function then reads the ICP balance from the victim's subaccount (L1153-1163), and if sufficient, registers the victim as a committed buyer (L1285-1288). The optional ticket system (L1248-1272) does not block this: the code explicitly states "If there exists no ticket for the buyer, the payment flow will simply ignore the ticket." A victim who has deposited ICP but not yet opened a ticket is fully exposed.

No caller-identity check exists anywhere in this code path (confirmed by grep across all swap Rust sources).

## Impact Explanation
This is a **High** severity finding matching: *"Unauthorized access to neurons, governance assets, wallets, identities, ledgers, or canister-controlled funds."* A victim who has deposited ICP into the swap subaccount (a normal intermediate state in the two-step participation flow) can have their ICP irreversibly committed to the swap without their authorization. If the swap reaches its minimum ICP target and commits, the victim's ICP is converted to SNS tokens and cannot be recovered. The confirmation text mechanism — the only consent gate for direct participation — is rendered meaningless.

## Likelihood Explanation
The attack requires only a standard ingress call to a public canister endpoint. No privileged access, no key material, no threshold corruption. The attacker reads `confirmation_text` from `get_init` (one query call), then calls `refresh_buyer_tokens` with `buyer = victim_principal` (one update call). The victim must have already deposited ICP, which is a normal and common intermediate state. The vulnerability window is the period between ICP deposit and the victim's own `refresh_buyer_tokens` call — potentially minutes to hours. Motivation exists for any party who wants a swap to commit (e.g., to reach the minimum ICP target).

## Recommendation
Add a caller-identity check in `refresh_buyer_tokens` before passing `p` to `refresh_buyer_token_e8s`:

```rust
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    let caller = caller_principal_id();
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller
    } else {
        let buyer = PrincipalId::from_str(&arg.buyer).unwrap();
        if buyer != caller {
            panic!("Caller {} is not authorized to refresh tokens on behalf of buyer {}", caller, buyer);
        }
        buyer
    };
    // ... rest unchanged
}
```

If proxy participation is a desired feature, it should follow the explicit allowlist pattern used in `rs/nns/cmc/src/main.rs` (`authorize_caller_to_call_notify_create_canister_on_behalf_of_creator`).

## Proof of Concept
**Preconditions:** SNS swap is in `Open` state with `confirmation_text = "I agree to the terms"`. Victim (`VICTIM`) has transferred ≥ `min_participant_icp_e8s` to the swap canister's ICP ledger subaccount for `VICTIM`, but has not yet called `refresh_buyer_tokens`.

**Attack:**
```bash
# Step 1: Read the public confirmation text
dfx canister call <swap_canister_id> get_init '(record {})'
# Returns: confirmation_text = opt "I agree to the terms"

# Step 2: Force victim's participation
dfx canister call <swap_canister_id> refresh_buyer_tokens '(record {
    buyer = "VICTIM_PRINCIPAL_TEXT_ID";
    confirmation_text = opt "I agree to the terms"
})'
```

**Result:** The swap canister reads the ICP balance from `VICTIM`'s subaccount, finds sufficient ICP, and registers `VICTIM` as a committed buyer. `VICTIM`'s ICP is now locked. If the swap commits, `VICTIM` receives SNS tokens instead of ICP — without ever having called `refresh_buyer_tokens` themselves or having agreed to the confirmation text.

This can be reproduced as a deterministic PocketIC integration test by: (1) initializing a swap with a `confirmation_text`, (2) simulating a victim ICP deposit to the subaccount, (3) calling `refresh_buyer_tokens` from a different principal with `buyer = victim`, and (4) asserting the victim appears in `list_direct_participants`.

### Citations

**File:** rs/sns/swap/canister/canister.rs (L130-134)
```rust
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()
    };
```

**File:** rs/sns/swap/src/swap.rs (L1149-1150)
```rust
        // User input validation doesn't expire after await, so this check doesn't need repetition.
        self.validate_confirmation_text(confirmation_text)?;
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
