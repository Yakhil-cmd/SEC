### Title
Anyone Can Force-Commit Another User's ICP Into an SNS Swap Without Their Consent - (`rs/sns/swap/canister/canister.rs`)

### Summary

The `refresh_buyer_tokens` endpoint in the SNS Swap canister accepts an arbitrary `buyer` principal in the request body without verifying that the caller is that buyer. Any unprivileged ingress sender can call this function with any victim's principal, forcing the victim's ICP — already sitting in their swap subaccount — to be registered as swap participation, locking those funds into the swap without the victim's explicit consent for the final commitment step.

### Finding Description

In `rs/sns/swap/canister/canister.rs`, the `refresh_buyer_tokens` update method resolves the buyer principal directly from the caller-supplied `arg.buyer` field with no authorization check:

```rust
#[update]
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()  // ← attacker-controlled
    };
    ...
    swap_mut()
        .refresh_buyer_token_e8s(p, arg.confirmation_text, this_canister_id(), &icp_ledger)
        .await
``` [1](#0-0) 

The resolved principal `p` is then passed directly into `refresh_buyer_token_e8s`, which reads the ICP balance of `swap_canister_subaccount[p]` on the ICP ledger and registers that amount as the buyer's committed participation in the swap:

```rust
let account = Account {
    owner: this_canister.get().0,
    subaccount: Some(principal_to_subaccount(&buyer)),
};
icp_ledger.account_balance(account).await ...
``` [2](#0-1) 

The `confirmation_text` field is validated against the SNS init payload, but that text is public on-chain, so it provides no real access control. [3](#0-2) 

The `RefreshBuyerTokensRequest` proto explicitly documents this open design: `"If not specified, the caller is used"` — meaning specifying a different buyer is an accepted input path with no restriction. [4](#0-3) 

### Impact Explanation

The SNS swap participation flow is:
1. Buyer calls `new_sale_ticket` to create a ticket.
2. Buyer transfers ICP to their dedicated subaccount of the swap canister on the ICP ledger.
3. Buyer calls `refresh_buyer_tokens` to register the participation.

Between steps 2 and 3, the buyer's ICP sits in the swap canister's subaccount. If the buyer changes their mind at this point, they can reclaim their ICP via `error_refund_icp` once the swap closes. However, if an attacker calls `refresh_buyer_tokens` with the victim's principal before the victim does, the victim's ICP is immediately registered as committed participation. Once registered:

- If the swap is **committed**, the victim receives SNS tokens instead of their ICP — they are forced into a swap they no longer wanted.
- The victim cannot reclaim their ICP until the swap finalizes.

This is the direct IC analog of the `repayLoan` bug: a function callable by anyone uses a stored/supplied account (not `msg.sender`/`caller()`) as the source of the financial action, causing that account owner to be committed to a financial outcome without their consent.

### Likelihood Explanation

**Medium.** The precondition is that the victim has completed step 2 (transferred ICP to the swap subaccount) but not yet step 3. This window exists in normal usage — users may transfer ICP and then pause before confirming. An attacker monitoring the ICP ledger for transfers to swap subaccounts can detect this window and immediately call `refresh_buyer_tokens` on the victim's behalf. The attack requires no privileged access, no key material, and no on-chain majority.

### Recommendation

Add a caller authorization check in `refresh_buyer_tokens`. When `arg.buyer` is non-empty, verify that the caller matches the specified buyer:

```rust
#[update]
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    let caller = caller_principal_id();
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller
    } else {
        let specified = PrincipalId::from_str(&arg.buyer).unwrap();
        if specified != caller {
            panic!("Caller is not authorized to refresh tokens for a different buyer");
        }
        specified
    };
    ...
}
``` [1](#0-0) 

### Proof of Concept

1. Alice (victim) calls `new_sale_ticket` on the SNS swap canister.
2. Alice transfers `min_participant_icp_e8s` ICP to her swap subaccount (`swap_canister_subaccount[alice]`) on the ICP ledger.
3. Alice decides not to participate and does **not** call `refresh_buyer_tokens`.
4. Bob (attacker), monitoring the ICP ledger, observes Alice's transfer and calls:
   ```
   refresh_buyer_tokens({ buyer = alice_principal; confirmation_text = <public_text_or_null> })
   ```
   from his own identity.
5. The swap canister reads Alice's subaccount balance, finds the ICP, and registers Alice as a committed participant.
6. When the swap commits, Alice receives SNS tokens instead of her ICP — she is forced into the swap against her will.

The system test in `rs/tests/nns/sns/lib/src/sns_deployment.rs` already demonstrates that calling `refresh_buyer_tokens` with a different principal's ID succeeds after that principal has transferred ICP, confirming the attack path is reachable on mainnet. [5](#0-4)

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
