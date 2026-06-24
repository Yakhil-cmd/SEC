Audit Report

## Title
Arbitrary Caller Can Commit Another User's ICP to SNS Swap Without Consent - (`rs/sns/swap/canister/canister.rs`)

## Summary
The `refresh_buyer_tokens` update method in the SNS Swap canister accepts an arbitrary `buyer` principal from the request body with no check that the caller equals the buyer. Because the `confirmation_text` is validated only against the public SNS init payload (not against the authenticated caller), any unprivileged ingress sender can call `refresh_buyer_tokens` with a victim's principal and the publicly known confirmation text, committing the victim's ICP to the swap without their explicit consent.

## Finding Description
In `rs/sns/swap/canister/canister.rs` at L130-133, the buyer principal is resolved from the request body rather than from the authenticated caller, with no authorization check:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    PrincipalId::from_str(&arg.buyer).unwrap()  // arbitrary principal, no auth check
};
``` [1](#0-0) 

A grep search for any caller-vs-buyer authorization check in `canister.rs` returns no matches, confirming no such guard exists. [2](#0-1) 

Inside `refresh_buyer_token_e8s`, the only consent gate is `validate_confirmation_text`, which compares the supplied string against the SNS init payload's public `confirmation_text` field — it does not verify that the entity supplying the text is the buyer: [3](#0-2) 

After this check passes, the function reads the ICP balance from the victim's subaccount and records it as committed participation: [4](#0-3) [5](#0-4) 

The code's own documentation confirms that a user who has transferred ICP but whose `refresh_buyer_tokens` was never successfully called can reclaim their ICP via `error_refund_icp` after the swap closes. Once Eve triggers `refresh_buyer_tokens` for Alice, this recovery path is foreclosed. [6](#0-5) 

## Impact Explanation
This is a **High** severity finding matching the allowed impact: "Significant SNS security impact with concrete user or protocol harm." A victim who transferred ICP to the swap canister's subaccount but chose not to participate (e.g., after reading and disagreeing with the confirmation text) can have their ICP irrevocably committed to the swap by any unprivileged third party. The confirmation text mechanism — the sole user-facing consent gate — is rendered meaningless. If the swap succeeds, the victim receives SNS tokens they never agreed to purchase and cannot recover their ICP.

## Likelihood Explanation
Medium-High. The precondition — that the victim has already transferred ICP to their swap subaccount — is a normal step in the participation flow, so many users will be in this state during an open swap. The attacker requires only the victim's principal (publicly observable on-chain) and the confirmation text (public in the SNS init payload). No privileged access, key material, or special conditions are required. Any unprivileged ingress sender can execute this against any victim currently in the pre-confirmation state.

## Recommendation
**Short term:** In `refresh_buyer_tokens`, enforce that when a non-empty `buyer` is supplied, the caller must equal the buyer principal:
```rust
if !arg.buyer.is_empty() && p != caller_principal_id() {
    panic!("Caller is not authorized to refresh tokens on behalf of another buyer.");
}
```
**Long term:** Remove the `buyer` field entirely and always derive the buyer from the authenticated caller. If third-party notification is needed (e.g., for automated bots), introduce an explicit allowlist of authorized notifier principals rather than permitting any caller to act on behalf of any buyer.

## Proof of Concept
1. Alice transfers 10 ICP to `swap_canister_subaccount(Alice)` on the ICP ledger during an open SNS swap with `confirmation_text = "I agree to the terms"`.
2. Alice reads the terms and decides not to participate. She does **not** call `refresh_buyer_tokens`.
3. Eve (any unprivileged principal) calls `refresh_buyer_tokens` with:
   ```
   RefreshBuyerTokensRequest {
       buyer: Alice.to_string(),
       confirmation_text: Some("I agree to the terms".to_string()),
   }
   ```
4. The canister resolves `p = Alice` (L133), passes `validate_confirmation_text` (L1150), reads Alice's subaccount balance (L1152-1163), and records Alice as a committed buyer (L1285-1291).
5. Alice's ICP is now locked in the swap. The `error_refund_icp` recovery path is foreclosed. If the swap commits, Alice receives SNS tokens she never consented to purchase.

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

**File:** rs/sns/swap/src/swap.rs (L363-384)
```rust
        pub fn validate_confirmation_text(
            &self,
            confirmation_text: Option<String>,
        ) -> Result<(), String> {
            match (
                self.init_or_panic().confirmation_text.as_ref(),
                confirmation_text,
            ) {
                (Some(expected_text), Some(text)) => {
                    if &text != expected_text {
                        Err("The value of `confirmation_text` does not match the value provided in SNS init payload.".to_string())
                    } else {
                        Ok(())
                    }
                }
                (Some(_), None) => Err("No value provided for `confirmation_text`.".to_string()),
                (None, Some(_)) => {
                    Err("Found a value for `confirmation_text`, expected none.".to_string())
                }
                (None, None) => Ok(()),
            }
        }
```

**File:** rs/sns/swap/src/swap.rs (L1125-1132)
```rust
    /// as an argument to this function (otherwise, the call will result in
    /// an error).
    ///
    /// If a ledger transfer was successfully made, but this call
    /// fails (many reasons are possible), the owner of the ICP sent
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
