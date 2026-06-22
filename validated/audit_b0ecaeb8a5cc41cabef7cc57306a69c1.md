### Title
Any Caller Can Register SNS Swap Participation for Any Buyer, Bypassing Confirmation Text Requirement - (File: rs/sns/swap/canister/canister.rs)

### Summary
The `refresh_buyer_tokens` endpoint in the SNS swap canister accepts a caller-supplied `buyer` principal with no authorization check, allowing any unprivileged ingress sender to register swap participation on behalf of any other principal. Combined with an attacker-controlled `confirmation_text` parameter, this allows an attacker to bypass the SNS-configured confirmation requirement and lock a victim's ICP into the swap without the victim's explicit agreement.

### Finding Description
In `rs/sns/swap/canister/canister.rs`, the `refresh_buyer_tokens` update method resolves the target buyer from the request payload rather than from the authenticated caller:

```rust
#[update]
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()  // no authorization check
    };
    ...
    swap_mut()
        .refresh_buyer_token_e8s(p, arg.confirmation_text, ...)
        .await
}
``` [1](#0-0) 

The `RefreshBuyerTokensRequest.buyer` field is documented as "If not specified, the caller is used," but when it is specified, there is no check that the caller equals the buyer. [2](#0-1) 

The `confirmation_text` field is passed directly to `refresh_buyer_token_e8s`, which validates it against the SNS-configured text: [3](#0-2) 

The configured confirmation text is a public parameter visible to anyone querying the swap canister's init data, so an attacker can trivially supply the correct value.

This is structurally identical to the LaunchBridge pattern: `transitionAll(address user, ...)` accepted a `user` argument with no caller check, letting any address trigger a state-changing operation on behalf of any user with attacker-chosen parameters.

### Impact Explanation
1. **Confirmation bypass**: An SNS can require participants to supply a specific `confirmation_text` to signal informed consent. An attacker who calls `refresh_buyer_tokens` with `buyer = victim` and the correct public confirmation text registers the victim's participation without the victim ever sending that text themselves, defeating the informed-consent mechanism.
2. **ICP locked into swap**: Once `refresh_buyer_token_e8s` records the buyer's participation, the victim's ICP (already sitting in the swap subaccount) is committed. The victim cannot reclaim it via `error_refund_icp` until the swap closes.
3. **Potential token loss**: If the swap is committed and the resulting SNS tokens trade below the ICP value at the time of participation, the victim suffers a financial loss they did not explicitly accept.

### Likelihood Explanation
Medium-low. The precondition is that the victim has already transferred ICP to the swap subaccount (a two-step flow: transfer then notify). A victim who transfers ICP but delays calling `refresh_buyer_tokens` — for example, while reviewing the confirmation text — is vulnerable during that window. The confirmation text is publicly readable from the swap canister's init parameters, so no privileged knowledge is required.

### Recommendation
- Enforce `caller == buyer` inside `refresh_buyer_tokens` when a non-empty `buyer` is supplied, or remove the `buyer` field entirely and always derive the buyer from the authenticated caller.
- If third-party notification is a desired feature (e.g., to allow a frontend canister to notify on behalf of a user), restrict it to an explicit allowlist of trusted callers rather than permitting any principal.

### Proof of Concept
1. SNS swap is open; swap init specifies `confirmation_text = "I agree to the terms"`.
2. Victim transfers 100 ICP to `swap_canister_subaccount(victim_principal)` on the ICP ledger, intending to review the terms before confirming.
3. Attacker (any principal) sends an ingress update to `swap_canister.refresh_buyer_tokens`:
   ```
   RefreshBuyerTokensRequest {
       buyer: "<victim_principal>",
       confirmation_text: Some("I agree to the terms"),
   }
   ```
4. `refresh_buyer_token_e8s` reads the victim's subaccount balance (100 ICP), validates the confirmation text (passes), and records the victim as a participant.
5. Victim's 100 ICP is now locked in the swap. If the swap commits, the victim receives SNS tokens they never explicitly agreed to purchase. [4](#0-3) [5](#0-4)

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
