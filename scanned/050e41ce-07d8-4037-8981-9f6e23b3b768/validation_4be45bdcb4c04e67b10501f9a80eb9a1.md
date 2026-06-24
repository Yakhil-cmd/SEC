### Title
Unauthenticated Caller Can Force SNS Swap Participation on Behalf of Any Principal, Bypassing Confirmation Text Consent — (File: rs/sns/swap/canister/canister.rs)

---

### Summary

The `refresh_buyer_tokens` update endpoint in the SNS Swap canister accepts a caller-supplied `buyer` principal string with no check that the caller matches the buyer. Any unprivileged ingress sender can invoke this function specifying an arbitrary victim principal, causing the swap canister to register that victim's ICP participation — including providing the SNS-required confirmation text on the victim's behalf — without the victim ever explicitly consenting.

---

### Finding Description

In `rs/sns/swap/canister/canister.rs`, the `refresh_buyer_tokens` function is an `#[update]` endpoint with no caller authorization check:

```rust
#[update]
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    log!(INFO, "refresh_buyer_tokens");
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()   // ← arbitrary principal, no caller == buyer check
    };
    let icp_ledger = create_real_icp_ledger(swap().init_or_panic().icp_ledger_or_panic());
    match swap_mut()
        .refresh_buyer_token_e8s(p, arg.confirmation_text, this_canister_id(), &icp_ledger)
        .await
    { ... }
}
``` [1](#0-0) 

The `RefreshBuyerTokensRequest` proto definition explicitly documents that `buyer` is optional and defaults to the caller if empty, but imposes no restriction on who may supply a non-empty value:

```proto
message RefreshBuyerTokensRequest {
  // If not specified, the caller is used.
  string buyer = 1;
  optional string confirmation_text = 2;
}
``` [2](#0-1) 

Inside `refresh_buyer_token_e8s`, the confirmation text supplied by the **attacker** (not the victim) is validated:

```rust
// User input validation doesn't expire after await, so this check doesn't need repetition.
self.validate_confirmation_text(confirmation_text)?;
``` [3](#0-2) 

The confirmation text is set at SNS initialization and is publicly visible on-chain. An attacker can therefore supply the correct text on behalf of any victim.

After the confirmation check, the function reads the ICP ledger balance of the victim's subaccount and, if sufficient, commits the victim's ICP to the swap:

```rust
let account = Account {
    owner: this_canister.get().0,
    subaccount: Some(principal_to_subaccount(&buyer)),
};
icp_ledger.account_balance(account).await ...
``` [4](#0-3) 

```rust
self.buyers
    .entry(buyer.to_string())
    .or_insert_with(|| BuyerState::new(0))
    .set_amount_icp_e8s(new_balance_e8s);
``` [5](#0-4) 

---

### Impact Explanation

1. **Forced participation without consent.** A victim who deposited ICP into their swap subaccount but then decided not to participate (e.g., because they did not wish to agree to the confirmation text) can be forcibly enrolled by any attacker who calls `refresh_buyer_tokens` with the victim's principal and the publicly known confirmation text. Once enrolled, the victim's ICP is committed to the swap and cannot be reclaimed until the swap reaches a terminal state (committed or aborted).

2. **Confirmation text consent bypass.** The confirmation text mechanism exists precisely to obtain explicit, in-band consent from the participant. Because the attacker — not the victim — supplies the text, the victim's ICP is committed without the victim ever agreeing to the terms.

3. **Griefing / timing attack.** A victim who sent ICP speculatively and wanted to withdraw before the swap filled can be forced into participation at the worst possible moment (e.g., just before the swap commits at an unfavorable token price).

---

### Likelihood Explanation

- The endpoint is a standard `#[update]` method reachable by any ingress sender with no `inspect_message` guard.
- The attacker needs only: (a) the victim's principal ID (publicly derivable from any prior on-chain interaction), (b) the confirmation text (stored in the swap's public `init` state), and (c) the victim to have ICP already sitting in their swap subaccount.
- No privileged role, key material, or subnet-majority corruption is required.

---

### Recommendation

1. When `arg.buyer` is non-empty, assert `caller_principal_id() == p` before proceeding. Third-party notification (the stated design intent) can be preserved by keeping the empty-buyer path that defaults to the caller.
2. Alternatively, remove the `buyer` override entirely and always derive the buyer from `caller_principal_id()`, consistent with how analogous notify endpoints (e.g., `notify_create_canister` in the CMC) enforce `caller == creator`.

---

### Proof of Concept

1. An SNS is deployed with `confirmation_text = "I agree to the SNS terms"`.
2. Alice sends 5 ICP to her swap subaccount (`subaccount = principal_to_subaccount(Alice)`), intending to evaluate before committing.
3. Alice decides not to participate and does not call `refresh_buyer_tokens`.
4. Bob (attacker) submits an ingress call:
   ```
   refresh_buyer_tokens({
     buyer: "<Alice's principal text>",
     confirmation_text: opt "I agree to the SNS terms"
   })
   ```
5. The swap canister reads Alice's subaccount balance (5 ICP ≥ `min_participant_icp_e8s`), validates the confirmation text Bob supplied, and commits Alice's 5 ICP to the swap — all without Alice ever calling the endpoint or agreeing to the terms.
6. Alice's ICP is now locked in the swap until it reaches a terminal lifecycle state.

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

**File:** rs/sns/swap/src/swap.rs (L1149-1150)
```rust
        // User input validation doesn't expire after await, so this check doesn't need repetition.
        self.validate_confirmation_text(confirmation_text)?;
```

**File:** rs/sns/swap/src/swap.rs (L1153-1163)
```rust
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

**File:** rs/sns/swap/src/swap.rs (L1285-1288)
```rust
        self.buyers
            .entry(buyer.to_string())
            .or_insert_with(|| BuyerState::new(0))
            .set_amount_icp_e8s(new_balance_e8s);
```
