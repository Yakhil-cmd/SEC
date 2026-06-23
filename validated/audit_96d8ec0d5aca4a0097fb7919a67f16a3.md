### Title
Unauthenticated Caller-Identity Spoofing in SNS Swap `refresh_buyer_tokens` Allows Forced Participation Without User Consent - (File: rs/sns/swap/canister/canister.rs)

---

### Summary

The `refresh_buyer_tokens` update method in the SNS Swap canister accepts an arbitrary `buyer` principal from the request payload and uses it—without verifying it matches the actual caller—to register ICP participation on behalf of that principal. Any unprivileged canister or user can call this endpoint specifying a victim's principal as the `buyer` field, forcing the victim's ICP (already sitting in their swap subaccount) into a committed participation state, bypassing any confirmation-text gate, and locking those funds until the swap finalizes.

---

### Finding Description

In `rs/sns/swap/canister/canister.rs`, the `refresh_buyer_tokens` handler reads the `buyer` field directly from the Candid argument:

```rust
#[update]
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()   // ← attacker-controlled
    };
    ...
    swap_mut()
        .refresh_buyer_token_e8s(p, arg.confirmation_text, this_canister_id(), &icp_ledger)
        .await
``` [1](#0-0) 

The Protobuf definition documents this explicitly:

```proto
message RefreshBuyerTokensRequest {
  // If not specified, the caller is used.
  string buyer = 1;
  optional string confirmation_text = 2;
}
``` [2](#0-1) 

Inside `refresh_buyer_token_e8s`, the supplied `buyer` principal is used without any caller-equality check to:
1. Look up the ICP balance of `subaccount(swap_canister, buyer)` on the ICP ledger.
2. Record that balance as the buyer's accepted participation in `self.buyers`. [3](#0-2) 

There is no guard of the form `require!(caller == buyer)`. The `confirmation_text` field is validated, but the confirmation text is a public string set at SNS initialization time and visible to anyone, so it provides no real barrier.

---

### Impact Explanation

**Forced participation / confirmation-text bypass.** A victim who has transferred ICP to `subaccount(swap_canister, victim_principal)` on the ICP ledger—but has not yet called `refresh_buyer_tokens` themselves (e.g., they changed their mind, or the SNS requires a confirmation text they have not agreed to)—can have their participation forcibly registered by any third-party canister. Once registered, the ICP is locked in the swap until finalization:

- If the swap **commits**, the victim receives SNS tokens instead of ICP, against their will.
- If the swap **aborts**, the victim eventually recovers ICP, but only after the swap closes.

The `error_refund_icp` path is only available after finalization, so there is no way for the victim to reclaim their ICP once `refresh_buyer_tokens` has been called on their behalf while the swap is still open. [4](#0-3) 

---

### Likelihood Explanation

The attack requires only that the victim has already sent ICP to the swap subaccount (a normal first step in participation). The attacker needs no privileged access: any canister or user can call `refresh_buyer_tokens` with an arbitrary `buyer` string. The confirmation text is publicly known. The attack is therefore trivially executable by any on-chain actor who monitors the ICP ledger for deposits to swap subaccounts.

---

### Recommendation

Enforce that the `buyer` field, when non-empty, must equal the actual `ic_cdk::caller()`. If third-party notification is a desired feature (e.g., to allow relayers), restrict it to an explicit allowlist or require the buyer to have pre-authorized the caller via a separate on-chain approval. Concretely:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    let requested = PrincipalId::from_str(&arg.buyer).unwrap();
    // Only allow a caller to refresh on behalf of themselves
    if requested != caller_principal_id() {
        panic!("caller is not authorized to refresh tokens for {}", requested);
    }
    requested
};
```

---

### Proof of Concept

1. Alice sends 10 ICP to `subaccount(swap_canister, alice_principal)` on the ICP ledger, intending to participate but wanting to review the confirmation text first.
2. Mallory's canister calls:
   ```
   refresh_buyer_tokens(RefreshBuyerTokensRequest {
       buyer: "alice_principal_text",
       confirmation_text: Some("<public SNS confirmation text>"),
   })
   ```
   with Mallory's own principal as the IC caller.
3. The swap canister reads Alice's subaccount balance (10 ICP), validates the (public) confirmation text, and records Alice as a committed buyer with 10 ICP.
4. Alice's ICP is now locked in the swap. She cannot reclaim it until the swap finalizes. If the swap commits, she receives SNS tokens she never explicitly agreed to purchase. [1](#0-0) [5](#0-4) [6](#0-5)

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

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L843-851)
```text
message RefreshBuyerTokensRequest {
  // If not specified, the caller is used.
  string buyer = 1;

  // To accept the swap participation confirmation, a participant should send
  // the confirmation text via refresh_buyer_tokens, matching the text set
  // during SNS initialization.
  optional string confirmation_text = 2;
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

**File:** rs/sns/swap/src/swap.rs (L1906-1925)
```rust
    /// Requests a refund of ICP tokens transferred to the Swap
    /// canister that was either never notified (via the
    /// refresh_buyer_tokens Candid method), or not fully accepted (by
    /// refresh_buyer_tokens).
    ///
    /// This method makes no changes (and instead panics) unless
    /// finalization has completed successfully (see the finalize
    /// method), which can only happen after self has entered the
    /// Aborted or Committed state.
    ///
    /// The entire balance in `subaccount(swap_canister, P)` is
    /// transferred to request.principal_id (minus the transfer fee,
    /// of course).
    ///
    /// This method is secure because it only transfers tokens from a
    /// principal's subaccount (of the Swap canister) to the
    /// principal's own account, i.e., the tokens were held in escrow
    /// for the principal (buyer) before the call and are returned to
    /// the same principal.
    pub async fn error_refund_icp(
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
