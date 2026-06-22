### Title
Missing Caller Ownership Check in `refresh_buyer_tokens` Allows Unauthorized Swap Participation Registration — (File: `rs/sns/swap/canister/canister.rs`)

---

### Summary

The SNS swap canister's `refresh_buyer_tokens` endpoint accepts an arbitrary `buyer` principal in its request without verifying that the caller is that buyer. Any unprivileged ingress sender can register another user's ICP participation in an SNS swap — including accepting a mandatory `confirmation_text` (terms-of-service) on the victim's behalf — as long as the victim has already transferred ICP to the swap canister's subaccount.

---

### Finding Description

The `refresh_buyer_tokens` update method in the SNS swap canister resolves the buyer principal from the caller-supplied `arg.buyer` string field without any ownership check: [1](#0-0) 

```rust
#[update]
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()  // ← no check that caller == p
    };
    ...
    swap_mut()
        .refresh_buyer_token_e8s(p, arg.confirmation_text, this_canister_id(), &icp_ledger)
        .await
```

The inner `refresh_buyer_token_e8s` function then:
1. Validates the `confirmation_text` against the SNS-configured expected text (a terms-of-service gate)
2. Reads the ICP balance of the **buyer's** subaccount on the swap canister
3. Writes the buyer's participation amount into `self.buyers` [2](#0-1) 

There is no check anywhere in this path that `caller == buyer`. The proto comment confirms the field is optional and defaults to the caller only when omitted: [3](#0-2) 

The `confirmation_text` is described as a mandatory consent gate that "a participant should confirm before they may participate": [4](#0-3) 

The validation only checks that the supplied text matches the SNS-configured text — it does not verify that the entity supplying the text is the buyer whose funds are being committed. [5](#0-4) 

---

### Impact Explanation

An attacker who knows a victim's principal ID (public on-chain information) and observes that the victim has transferred ICP to the swap canister's subaccount (visible via ledger queries) can:

1. Call `refresh_buyer_tokens` with `buyer = <victim_principal>` and the correct `confirmation_text` (which is public, set at SNS initialization time).
2. The swap canister reads the victim's ICP balance, validates the attacker-supplied confirmation text, and writes the victim's participation into `self.buyers`. [6](#0-5) 

If the swap subsequently commits, the victim's ICP is swept to the SNS treasury and the victim receives SNS tokens — a financial outcome they never explicitly consented to. The `confirmation_text` mechanism, which exists precisely to record explicit user consent to swap terms, is entirely bypassed.

---

### Likelihood Explanation

- The attacker requires no privileged access: only a valid ingress identity.
- The victim's principal is public; their ICP subaccount balance on the swap canister is queryable.
- The `confirmation_text` is set at SNS initialization and is publicly readable from the swap canister's `get_init` query endpoint.
- The scenario where a user transfers ICP to the swap subaccount but has not yet called `refresh_buyer_tokens` (e.g., multi-step UI flow, accidental transfer, or deliberate delay pending review of terms) is realistic and common.

---

### Recommendation

Add a check in `refresh_buyer_tokens` (or in `refresh_buyer_token_e8s`) that, when a non-empty `buyer` is specified, the caller must equal the buyer:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    let specified = PrincipalId::from_str(&arg.buyer).unwrap();
    if specified != caller_principal_id() {
        ic_cdk::trap("caller must match the specified buyer");
    }
    specified
};
```

This mirrors the fix applied in the referenced Solana restaking audit (checking that the caller owns the relevant account before mutating state on their behalf).

---

### Proof of Concept

1. Victim transfers 100 ICP to `swap_canister_subaccount(victim_principal)` on the ICP ledger.
2. Victim has not yet called `refresh_buyer_tokens` (e.g., is reviewing the SNS terms).
3. Attacker reads the SNS `confirmation_text` via `get_init` (public query).
4. Attacker calls:
   ```
   refresh_buyer_tokens({
     buyer: "<victim_principal>",
     confirmation_text: opt "<correct_text>"
   })
   ```
5. Swap canister registers victim's 100 ICP participation with the attacker-supplied consent.
6. If the swap commits, victim's ICP is swept; victim receives SNS tokens without having agreed to the terms themselves. [7](#0-6) [8](#0-7)

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

**File:** rs/sns/swap/src/swap.rs (L359-384)
```rust
        /// Validate the confirmation text from the caller who wishes to participate in the swap.
        /// This is conceptually just comparing the text against what has been specified in
        /// the SnsInitPayload structure, but we provide precise errors in case something
        /// does not match.
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

**File:** rs/sns/swap/src/swap.rs (L1274-1291)
```rust
        // Append to a new buyer to the BUYERS_LIST_INDEX
        let is_preexisting_buyer = self.buyers.contains_key(&buyer.to_string());
        if !is_preexisting_buyer {
            insert_buyer_into_buyers_list_index(buyer)
                .map_err(|grow_failed| {
                    format!(
                        "Failed to add buyer {buyer} to state, the canister's stable memory could not grow: {grow_failed}"
                    )
                })?;
        }

        self.buyers
            .entry(buyer.to_string())
            .or_insert_with(|| BuyerState::new(0))
            .set_amount_icp_e8s(new_balance_e8s);
        // We compute the current participation amounts once and store the result in Swap's state,
        // for efficiency reasons.
        self.update_total_participation_amounts();
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
