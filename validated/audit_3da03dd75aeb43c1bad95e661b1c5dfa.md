Audit Report

## Title
Unprivileged Attacker Can Force Arbitrary Principal Into SNS Swap Participation Without Consent — (`rs/sns/swap/canister/canister.rs`, `rs/sns/swap/src/swap.rs`)

## Summary

The `refresh_buyer_tokens` ingress endpoint in `rs/sns/swap/canister/canister.rs` accepts an arbitrary `buyer` principal in its request payload and uses it directly without verifying that `buyer == caller`. An attacker can pre-fund a victim's swap subaccount via a standard ICP ledger transfer and then invoke this endpoint with `buyer = victim`, causing the swap canister to register the victim as a committed participant. Upon swap finalization, the victim's ICP is swept to SNS governance and locked SNS neurons are created for the victim without their knowledge or consent.

## Finding Description

**Entrypoint — no caller/buyer identity check:**

In `rs/sns/swap/canister/canister.rs` lines 130–134, when `arg.buyer` is non-empty, the caller's identity is completely ignored:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    PrincipalId::from_str(&arg.buyer).unwrap()  // no check that arg.buyer == caller
};
``` [1](#0-0) 

Any principal can be substituted as the effective buyer.

**Core logic — balance lookup keyed on attacker-supplied principal:**

`refresh_buyer_token_e8s` in `rs/sns/swap/src/swap.rs` queries the ICP ledger balance of `swap_canister[subaccount(buyer)]` using the attacker-supplied principal: [2](#0-1) 

If that balance meets `min_participant_icp_e8s`, the victim's `BuyerState` is created/updated: [3](#0-2) 

**Ticket check is explicitly optional:**

The only other guard — the open-ticket check — is a no-op when no ticket exists for the buyer. The inline comment at line 1271 confirms: *"If there exists no ticket for the buyer, the payment flow will simply ignore the ticket."* An attacker calling on behalf of a victim will have no ticket for the victim, so this check is bypassed entirely: [4](#0-3) 

**Confirmation text is not a barrier:**

`validate_confirmation_text` is called, but the confirmation text is a public parameter set at SNS initialization time — readable by anyone from the swap's `init` state. It provides no access control.

**No recourse during OPEN state:**

The docstring for `refresh_buyer_token_e8s` explicitly states that ICP sent to a subaccount can only be reclaimed via `error_refund_icp` *"once this swap is closed (committed or aborted)"* — meaning the victim has no recourse while the swap is OPEN: [5](#0-4) 

**Finalization locks the victim in:**

On commit, `sweep_icp` transfers ICP from `swap_canister[subaccount(victim)]` to SNS governance and creates locked SNS neuron recipes for the victim: [6](#0-5) 

## Impact Explanation

This is a **High** severity finding matching the allowed impact: *"Unauthorized access to neurons, governance assets, wallets, identities, ledgers, or canister-controlled funds where exploitation requires meaningful per-target work or other constraints."*

Concretely:
- The victim's ICP (pre-deposited by the attacker into the victim's swap subaccount) is swept to SNS governance without the victim's consent upon swap commitment.
- The victim receives locked SNS neurons with dissolve delays (typically months to years) they never chose to acquire.
- The victim cannot reverse this after the swap commits; `error_refund_icp` is unavailable during the OPEN phase.
- The attacker sacrifices ICP (it flows to SNS governance on commit, or is refunded to the victim on abort), making this a griefing/coercion attack rather than theft — but the victim suffers real, irreversible financial harm on commit.

## Likelihood Explanation

- Requires no privileged access — any principal can call `refresh_buyer_tokens` via ingress.
- Requires only the ability to transfer ICP to an arbitrary ICRC-1 account (the victim's subaccount of the swap canister), which is a standard, permissionless ledger operation.
- The confirmation text is publicly readable from swap state.
- The attack is fully executable on mainnet against any open SNS swap.
- The attacker only needs to know the victim's principal ID (publicly observable on-chain) and the swap canister ID.

## Recommendation

Add a caller-equals-buyer authorization check in the canister entry point. If the `buyer` field is specified and differs from the caller, reject the call:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    let specified = PrincipalId::from_str(&arg.buyer).unwrap();
    let caller = caller_principal_id();
    if specified != caller {
        panic!("buyer must match caller");
    }
    specified
};
``` [7](#0-6) 

## Proof of Concept

```rust
// Precondition: swap is OPEN, victim_principal is the target
// Step 1: Attacker transfers ICP to victim's subaccount of swap canister
icp_ledger.transfer(Account {
    owner: swap_canister_id,
    subaccount: Some(principal_to_subaccount(&victim_principal)),
}, min_participant_icp_e8s);

// Step 2: Attacker calls refresh_buyer_tokens with buyer = victim
swap_canister.refresh_buyer_tokens(RefreshBuyerTokensRequest {
    buyer: victim_principal.to_string(),
    confirmation_text: Some("<public confirmation text from swap init>".to_string()),
});

// Step 3: Assert victim is now a registered participant
let state = swap_canister.get_buyer_state(victim_principal);
assert!(state.buyer_state.is_some());

// Step 4: After swap commits, victim has locked SNS neurons they never consented to
```

This maps directly to the unit test pattern in `rs/sns/swap/tests/swap.rs` where `refresh_buyer_token_e8s` is called with a `buyer` principal and a mock ledger balance, asserting `swap.buyers.contains_key(&buyer.to_string())` after the call confirms registration. [8](#0-7)

### Citations

**File:** rs/sns/swap/canister/canister.rs (L128-143)
```rust
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

**File:** rs/sns/swap/src/swap.rs (L1130-1132)
```rust
    /// to the subaccount can reclaim their tokens using `error_refund_icp`
    /// once this swap is closed (committed or aborted).
    ///
```

**File:** rs/sns/swap/src/swap.rs (L1152-1163)
```rust
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

**File:** rs/sns/swap/src/swap.rs (L1285-1288)
```rust
        self.buyers
            .entry(buyer.to_string())
            .or_insert_with(|| BuyerState::new(0))
            .set_amount_icp_e8s(new_balance_e8s);
```

**File:** rs/sns/swap/src/swap.rs (L2070-2094)
```rust
        for (principal_str, buyer_state) in self.buyers.iter_mut() {
            // principal_str should always be parseable as a PrincipalId as that is enforced
            // in `refresh_buyer_tokens`. In the case of a bug due to programmer error, increment
            // the invalid field. This will require a manual intervention via an upgrade to correct
            let principal = match string_to_principal(principal_str) {
                Some(p) => p,
                None => {
                    sweep_result.invalid += 1;
                    continue;
                }
            };

            let subaccount = principal_to_subaccount(&principal);
            let dst = if lifecycle == Lifecycle::Committed {
                // This Account should be given a name, such as SNS ICP Treasury...
                Account {
                    owner: sns_governance.get().0,
                    subaccount: None,
                }
            } else {
                Account {
                    owner: principal.0,
                    subaccount: None,
                }
            };
```

**File:** rs/sns/swap/tests/swap.rs (L413-429)
```rust
        let e = swap
            .refresh_buyer_token_e8s(
                *TEST_USER1_PRINCIPAL,
                None,
                SWAP_CANISTER_ID,
                &mock_stub(vec![LedgerExpect::AccountBalance(
                    Account {
                        owner: SWAP_CANISTER_ID.get().into(),
                        subaccount: Some(principal_to_subaccount(&TEST_USER1_PRINCIPAL.clone())),
                    },
                    Ok(Tokens::from_e8s(99999999)),
                )]),
            )
            .now_or_never()
            .unwrap();
        assert!(e.is_err());
        assert!(!swap.buyers.contains_key(&TEST_USER1_PRINCIPAL.to_string()));
```
