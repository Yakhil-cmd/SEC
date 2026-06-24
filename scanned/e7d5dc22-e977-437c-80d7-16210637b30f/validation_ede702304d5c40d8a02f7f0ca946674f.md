### Title
Caller Identity Not Verified in `refresh_buyer_tokens` Allows Forced SNS Swap Participation and Confirmation Text Bypass - (File: rs/sns/swap/canister/canister.rs)

### Summary
The `refresh_buyer_tokens` update method in the SNS Swap canister accepts an arbitrary `buyer` principal in its request argument and uses it directly without verifying that it matches the actual caller. Any unprivileged ingress sender can call this method with a victim's principal as the `buyer` field, forcing the victim's ICP (already deposited in the swap subaccount) to be registered as a confirmed participation — bypassing the SNS-configured confirmation text consent mechanism.

### Finding Description
In `rs/sns/swap/canister/canister.rs`, the `refresh_buyer_tokens` endpoint reads the `buyer` field from the caller-supplied argument and, if non-empty, uses it as the principal whose participation is being registered — with no check that `buyer == caller()`:

```rust
#[update]
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()  // ← no caller == buyer check
    };
    ...
    swap_mut()
        .refresh_buyer_token_e8s(p, arg.confirmation_text, this_canister_id(), &icp_ledger)
        .await
``` [1](#0-0) 

The inner `refresh_buyer_token_e8s` function then reads the ICP balance from the subaccount derived from the supplied `buyer` principal and records that principal's participation in `self.buyers[buyer]`:

```rust
let account = Account {
    owner: this_canister.get().0,
    subaccount: Some(principal_to_subaccount(&buyer)),
};
icp_ledger.account_balance(account).await ...
``` [2](#0-1) 

The `RefreshBuyerTokensRequest` proto explicitly documents that the `buyer` field defaults to the caller only when empty, making the caller-supplied override an intentional design — but without any authorization guard: [3](#0-2) 

### Impact Explanation
An SNS can require participants to supply a specific `confirmation_text` as an explicit consent signal before their ICP is accepted. An attacker who knows (or can guess) the confirmation text can call `refresh_buyer_tokens` with:
- `buyer` = victim's principal
- `confirmation_text` = the required consent string

This registers the victim's ICP deposit as a confirmed participation without the victim ever calling the endpoint themselves. If the swap reaches the `COMMITTED` lifecycle, the victim's ICP is swept to the SNS treasury and they receive SNS tokens instead — an outcome they never consented to. The victim cannot recover their ICP once the swap is committed.

Even without a confirmation text, an attacker can trigger participation registration for any principal that has ICP sitting in the swap subaccount, removing the victim's ability to reclaim their ICP via the normal `error_refund_icp` path (which only applies before participation is registered).

### Likelihood Explanation
The confirmation text for an SNS swap is public — it is set in the SNS initialization parameters and visible on-chain. Any unprivileged ingress sender can call `refresh_buyer_tokens` with an arbitrary `buyer` string. No special privilege, key, or majority is required. The only precondition is that the victim has already transferred ICP to the swap subaccount (a normal step in the participation flow), which is also observable on-chain.

### Recommendation
Add a caller authorization check in the canister endpoint before using the `buyer` field:

```rust
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    let caller = caller_principal_id();
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller
    } else {
        let buyer = PrincipalId::from_str(&arg.buyer).unwrap();
        if buyer != caller {
            panic!("Caller {} is not authorized to refresh tokens for buyer {}", caller, buyer);
        }
        buyer
    };
    ...
}
```

Alternatively, remove the `buyer` override entirely and always derive the principal from `caller()`, since the only legitimate use case is a participant registering their own deposit.

### Proof of Concept
1. Alice transfers ICP to the SNS Swap canister subaccount derived from her principal (the normal first step of participation).
2. The SNS requires a `confirmation_text` of `"I agree to the terms"`.
3. Attacker Eve (any principal) calls:
   ```
   refresh_buyer_tokens({
     buyer: "<Alice's principal text>",
     confirmation_text: Some("I agree to the terms")
   })
   ```
4. The swap canister reads Alice's subaccount balance, validates the confirmation text (which Eve supplied), and records Alice as a confirmed participant in `self.buyers`.
5. When the swap commits, Alice's ICP is swept to the SNS treasury and she receives SNS tokens — without ever having called `refresh_buyer_tokens` herself or consented to participation.

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
