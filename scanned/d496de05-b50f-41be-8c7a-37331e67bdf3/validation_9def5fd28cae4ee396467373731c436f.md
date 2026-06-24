### Title
Arbitrary Caller Can Commit Another User's ICP to SNS Swap Without Consent, Bypassing Confirmation Text - (`rs/sns/swap/canister/canister.rs`)

### Summary
The `refresh_buyer_tokens` endpoint in the SNS Swap canister accepts an arbitrary `buyer` principal in the request body with no check that the caller equals the buyer. Combined with the fact that the `confirmation_text` consent mechanism is validated only against the public SNS init payload (not against the actual caller), any unprivileged ingress sender can commit another user's ICP to a swap and satisfy the confirmation text on their behalf, without the victim ever explicitly consenting.

### Finding Description
In `rs/sns/swap/canister/canister.rs`, the `refresh_buyer_tokens` update method resolves the buyer principal from the request body rather than from the authenticated caller:

```rust
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()  // arbitrary principal, no auth check
    };
    ...
    swap_mut()
        .refresh_buyer_token_e8s(p, arg.confirmation_text, this_canister_id(), &icp_ledger)
        .await
``` [1](#0-0) 

The `RefreshBuyerTokensRequest` proto documents this as intentional ("If not specified, the caller is used"), but imposes no restriction on who may supply a non-empty `buyer` field: [2](#0-1) 

Inside `refresh_buyer_token_e8s`, the confirmation text is validated only by comparing it against the SNS init payload's public `confirmation_text` string — there is no check that the entity providing the text is the buyer:

```rust
// User input validation doesn't expire after await, so this check doesn't need repetition.
self.validate_confirmation_text(confirmation_text)?;
``` [3](#0-2) 

`validate_confirmation_text` simply compares the supplied string against the SNS-configured expected text: [4](#0-3) 

Because the confirmation text is part of the public SNS init payload, any caller who knows it (which is everyone) can supply it on behalf of any victim.

After the confirmation text check passes, the function reads the ICP balance from the victim's subaccount on the swap canister and records it as the victim's committed participation: [5](#0-4) [6](#0-5) 

### Impact Explanation
A user (Alice) who transferred ICP to the swap canister's subaccount but then decided not to participate — for example, because she read the confirmation text and disagreed with the terms — can have her ICP committed to the swap by any third party (Eve). Eve calls `refresh_buyer_tokens` with `buyer = Alice` and the correct public confirmation text. Alice's ICP is now recorded as committed participation. Alice can no longer use `error_refund_icp` to reclaim her ICP (that path is only available when `refresh_buyer_tokens` was never successfully called for the buyer). If the swap succeeds, Alice receives SNS tokens she never consented to receive. The confirmation text consent mechanism — the only user-facing gate for explicit agreement to swap terms — is rendered meaningless.

### Likelihood Explanation
Medium. The precondition is that the victim has already transferred ICP to the swap canister's buyer-specific subaccount. This is a normal step in the participation flow, so many victims will be in this state during an open swap. The attacker needs only the victim's principal (publicly observable on-chain) and the confirmation text (public in the SNS init payload). No privileged access, key material, or majority corruption is required. Any unprivileged ingress sender can execute this.

### Recommendation
- **Short term:** In `refresh_buyer_tokens`, enforce that when a non-empty `buyer` is supplied, the caller must equal the buyer principal. Reject the call otherwise:
  ```rust
  if !arg.buyer.is_empty() && p != caller_principal_id() {
      panic!("Caller is not authorized to refresh tokens on behalf of another buyer.");
  }
  ```
- **Long term:** Redesign the `buyer` field to be removed entirely, always deriving the buyer from the authenticated caller. If third-party notification is needed (e.g., for automated bots), introduce an explicit allowlist of authorized notifier principals rather than allowing any caller to act on behalf of any buyer.

### Proof of Concept
1. Alice transfers 10 ICP to `swap_canister_subaccount(Alice)` on the ICP ledger, intending to participate in an SNS swap that requires `confirmation_text = "I agree to the terms"`.
2. Alice reads the terms and decides not to participate. She does **not** call `refresh_buyer_tokens`.
3. Eve (any unprivileged principal) calls `refresh_buyer_tokens` on the swap canister with:
   ```
   RefreshBuyerTokensRequest {
       buyer: Alice.to_string(),
       confirmation_text: Some("I agree to the terms".to_string()),
   }
   ```
4. The swap canister resolves `p = Alice`, validates the confirmation text (passes, since it matches the public init payload), reads Alice's subaccount balance (10 ICP), and records Alice as a committed buyer.
5. Alice's ICP is now locked in the swap. She cannot use `error_refund_icp` to reclaim it. If the swap commits, she receives SNS tokens she never explicitly agreed to purchase.

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

**File:** rs/sns/swap/src/gen/ic_sns_swap.pb.v1.rs (L1117-1126)
```rust
pub struct RefreshBuyerTokensRequest {
    /// If not specified, the caller is used.
    #[prost(string, tag = "1")]
    pub buyer: ::prost::alloc::string::String,
    /// To accept the swap participation confirmation, a participant should send
    /// the confirmation text via refresh_buyer_tokens, matching the text set
    /// during SNS initialization.
    #[prost(string, optional, tag = "2")]
    pub confirmation_text: ::core::option::Option<::prost::alloc::string::String>,
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

**File:** rs/sns/swap/src/swap.rs (L1149-1150)
```rust
        // User input validation doesn't expire after await, so this check doesn't need repetition.
        self.validate_confirmation_text(confirmation_text)?;
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
