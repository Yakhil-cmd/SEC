### Title
Lack of Access Control in `refresh_buyer_tokens` Allows Anyone to Record Participation for Any Buyer — (File: `rs/sns/swap/canister/canister.rs`)

---

### Summary

The `refresh_buyer_tokens` update endpoint in the SNS Swap canister accepts a `buyer` principal as a string parameter but performs no check that the caller equals the specified buyer. Any unprivileged ingress sender can invoke this function on behalf of any buyer, recording their ICP participation and — critically — satisfying the optional `confirmation_text` consent gate without the buyer's explicit action.

---

### Finding Description

In `rs/sns/swap/canister/canister.rs`, the `refresh_buyer_tokens` handler resolves the target buyer from the request argument rather than from the authenticated caller:

```rust
#[update]
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()   // ← no check: caller == p
    };
    let icp_ledger = create_real_icp_ledger(swap().init_or_panic().icp_ledger_or_panic());
    match swap_mut()
        .refresh_buyer_token_e8s(p, arg.confirmation_text, this_canister_id(), &icp_ledger)
        .await
    { ... }
}
``` [1](#0-0) 

There is no assertion that `caller_principal_id() == p`. The `RefreshBuyerTokensRequest` proto documents the field as "If not specified, the caller is used," but when it IS specified, no authorization check is enforced. [2](#0-1) 

The inner function `refresh_buyer_token_e8s` reads the ICP balance of the buyer's subaccount on the swap canister and records their participation amount in `self.buyers`. If the SNS was initialized with a required `confirmation_text`, this function also validates that the provided text matches. Because the confirmation text is stored in the swap canister's publicly-readable state, any caller can supply the correct text on behalf of any buyer. [3](#0-2) 

The design intent of allowing third-party callers is confirmed by a system test that explicitly calls `refresh_buyer_tokens` for a different user (`wealthy_user_identity`) from a `default_sns_agent`: [4](#0-3) 

---

### Impact Explanation

An unprivileged attacker can:

1. **Force participation recording without the buyer's explicit action.** A buyer who has deposited ICP into the swap subaccount but has not yet decided to confirm (e.g., still reading terms, waiting to see if the swap will succeed, or deposited by mistake) can have their participation committed by a third party.

2. **Bypass the `confirmation_text` consent mechanism.** The confirmation text is the only on-chain signal that a buyer has explicitly agreed to the swap terms. Because the text is public (readable from swap state) and the caller identity is not checked, any attacker can supply the correct text on behalf of any buyer, recording their participation as if they had confirmed. This nullifies the purpose of the confirmation gate.

The analogy to the external report is direct: just as `withdrawInterest` could be called for any lender at a time not in their interest, `refresh_buyer_tokens` can be called for any buyer at a time not in their interest — and additionally bypasses an explicit-consent mechanism.

---

### Likelihood Explanation

**Medium.** The attacker requires only:
- The target buyer's principal (publicly derivable from their subaccount in the swap canister's state).
- The confirmation text (publicly readable from the swap canister's `get_init` or `get_sale_parameters` query endpoints).
- The buyer to have already deposited ICP into the swap subaccount (a prerequisite the buyer themselves created).

No privileged access, key material, or subnet-majority corruption is needed. The attack is a single ingress update call.

---

### Recommendation

Add a caller-equals-buyer check in the canister endpoint, mirroring the fix recommended in the external report:

```rust
#[update]
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    let caller = caller_principal_id();
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller
    } else {
        let buyer = PrincipalId::from_str(&arg.buyer).unwrap();
        assert_eq!(
            buyer, caller,
            "Caller {} is not authorized to refresh tokens for buyer {}",
            caller, buyer
        );
        buyer
    };
    // ... rest unchanged
}
```

If third-party notification is a desired composability feature (as the Sublime team noted), a separate permissioned path (e.g., restricted to a known aggregator canister) should be provided rather than leaving the endpoint fully open.

---

### Proof of Concept

1. Alice sends 100 ICP to the swap canister's subaccount derived from her principal (a deliberate deposit, but she has not yet confirmed).
2. The SNS was initialized with `confirmation_text = "I agree to the SNS swap terms"`.
3. Bob (attacker) calls `swap.get_init()` (a public query) to retrieve the confirmation text.
4. Bob submits an ingress update:
   ```
   refresh_buyer_tokens({
     buyer: "<Alice's principal>",
     confirmation_text: "I agree to the SNS swap terms"
   })
   ```
5. The swap canister reads Alice's subaccount balance (100 ICP), records her participation in `self.buyers`, and marks her as having confirmed — all without Alice ever calling the endpoint herself.
6. Alice's ICP is now committed to the swap as a confirmed participant, bypassing the explicit-consent gate the SNS deployer intended. [1](#0-0) [5](#0-4) [6](#0-5)

### Citations

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

**File:** rs/tests/nns/sns/lib/src/sns_deployment.rs (L919-944)
```rust
    // Use the default identity to call refresh_buyer_tokens for the wealthy user
    let res_4 = {
        let request = sns_request_provider
            .refresh_buyer_tokens(Some(wealthy_user_identity.principal_id), None);
        block_on(default_sns_agent.call_and_parse(&request))
            .result()
            .unwrap()
    };
    info!(
        log,
        "Third update call to `sns_sale.refresh_buyer_tokens` returned {res_4:?} (elapsed {:?})",
        start_time.elapsed()
    );
    // Use the wealthy user's identity to call refresh_buyer_tokens for "self"
    let res_5 = {
        let request = sns_request_provider.refresh_buyer_tokens(None, None);
        block_on(wealthy_sns_agent.call_and_parse(&request))
            .result()
            .unwrap()
    };
    info!(
        log,
        "Fourth update call to `sns_sale.refresh_buyer_tokens` returned {res_5:?} (elapsed {:?})",
        start_time.elapsed()
    );
    assert_eq!(
```

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L839-855)
```text
// Informs the swap canister that a buyer has sent funds to participate in the
// swap.
//
// Only in lifecycle state `open`.
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
