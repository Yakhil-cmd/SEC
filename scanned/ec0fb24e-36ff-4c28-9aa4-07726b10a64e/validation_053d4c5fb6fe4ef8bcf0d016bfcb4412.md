### Title
Missing Caller Validation in `refresh_buyer_tokens` Allows Forced Participation and Confirmation-Text Bypass - (File: rs/sns/swap/canister/canister.rs)

### Summary
The SNS Swap canister's `refresh_buyer_tokens` update method accepts an arbitrary `buyer` principal in its request payload without verifying that the caller matches the specified buyer. Any unprivileged ingress sender can call this method with any victim's principal as the `buyer`, registering that victim's ICP participation and bypassing the SNS's confirmation-text consent mechanism on their behalf.

### Finding Description

The `refresh_buyer_tokens` endpoint in the SNS Swap canister is an `#[update]` method callable by any ingress sender:

```rust
// rs/sns/swap/canister/canister.rs L127-143
#[update]
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    log!(INFO, "refresh_buyer_tokens");
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()  // ← no check that caller == buyer
    };
    ...
    swap_mut()
        .refresh_buyer_token_e8s(p, arg.confirmation_text, this_canister_id(), &icp_ledger)
        .await
``` [1](#0-0) 

The `RefreshBuyerTokensRequest` proto explicitly documents this design:

```
// If not specified, the caller is used.
string buyer = 1;
optional string confirmation_text = 2;
``` [2](#0-1) 

When `buyer` is non-empty, the function uses the attacker-supplied principal as the buyer identity with **no check** that `caller == buyer`. The underlying `refresh_buyer_token_e8s` then:

1. Reads the ICP ledger balance of `buyer`'s subaccount on the swap canister.
2. Validates the attacker-supplied `confirmation_text` against the SNS-configured required text.
3. Registers the buyer's participation in `self.buyers` and updates total participation amounts. [3](#0-2) [4](#0-3) 

The confirmation text is a consent mechanism set during SNS initialization and is publicly readable. Any attacker who knows it (which is everyone) can supply it on behalf of any victim.

### Impact Explanation

**Forced participation registration**: If a victim has transferred ICP to their swap subaccount (e.g., to evaluate participation) but has not yet called `refresh_buyer_tokens` themselves, an attacker can call the method with `buyer = victim_principal` and the correct `confirmation_text`. This registers the victim's participation in the swap's `buyers` map, locking their ICP into the swap without the victim's explicit consent via the confirmation-text mechanism.

**Confirmation-text bypass**: The confirmation text (`confirmation_text` field) is the SNS's only on-chain consent signal. By supplying it on behalf of an arbitrary `buyer`, an attacker circumvents this consent gate entirely for any principal whose ICP is already sitting in the swap subaccount.

**Swap lifecycle manipulation**: An attacker can trigger participation for many victims simultaneously, potentially pushing the swap's `direct_participation_icp_e8s` over the committed threshold and altering the swap's lifecycle outcome. [5](#0-4) 

### Likelihood Explanation

**Medium.** The attack requires the victim to have already transferred ICP to their swap subaccount but not yet called `refresh_buyer_tokens`. This window exists in the normal payment flow (transfer ICP → call `refresh_buyer_tokens`). The confirmation text is public (set in SNS init args, readable via `get_init`). No privileged access is required; any ingress sender can execute this. [6](#0-5) 

### Recommendation

Add a caller-identity check when `buyer` is explicitly specified:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    let specified = PrincipalId::from_str(&arg.buyer).unwrap();
    // Require caller == buyer when buyer is explicitly set
    if specified != caller_principal_id() {
        panic!("Caller must match the specified buyer");
    }
    specified
};
```

Alternatively, remove the `buyer` field entirely and always derive the buyer from `caller_principal_id()`, consistent with how other sensitive swap methods operate.

### Proof of Concept

1. Victim transfers `min_participant_icp_e8s` ICP to the swap canister's subaccount for their principal (the normal first step of participation).
2. Victim has not yet called `refresh_buyer_tokens` (they are still deciding whether to confirm the swap terms).
3. Attacker reads the SNS's required `confirmation_text` from the public `get_init` endpoint.
4. Attacker submits an ingress update call to the swap canister:
   ```
   refresh_buyer_tokens({
     buyer: "<victim_principal_text>",
     confirmation_text: Some("<required_confirmation_text>")
   })
   ```
5. The swap canister reads the victim's ICP balance (≥ `min_participant_icp_e8s`), validates the attacker-supplied confirmation text, and registers the victim as a participant in `self.buyers` — all without the victim's explicit consent.
6. The victim's ICP is now committed to the swap. If the swap commits, the victim receives SNS tokens; if aborted, they must use `error_refund_icp`. In either case, the victim's consent (the confirmation text) was provided by the attacker, not the victim. [7](#0-6) [8](#0-7)

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

**File:** rs/sns/swap/src/swap.rs (L1200-1213)
```rust
        // Check that the minimum amount has been transferred before
        // actually creating an entry for the buyer.
        if e8s < params.min_participant_icp_e8s {
            return Err(format!(
                "Amount transferred: {}; minimum required to participate: {}",
                e8s, params.min_participant_icp_e8s
            ));
        }
        let max_participant_icp_e8s = params.max_participant_icp_e8s;

        let old_amount_icp_e8s = self
            .buyers
            .get(&buyer.to_string())
            .map_or(0, |buyer| buyer.amount_icp_e8s());
```

**File:** rs/sns/swap/src/swap.rs (L1285-1308)
```rust
        self.buyers
            .entry(buyer.to_string())
            .or_insert_with(|| BuyerState::new(0))
            .set_amount_icp_e8s(new_balance_e8s);
        // We compute the current participation amounts once and store the result in Swap's state,
        // for efficiency reasons.
        self.update_total_participation_amounts();

        log!(
            INFO,
            "Refresh_buyer_tokens for buyer {}; old e8s {}; new e8s {}",
            buyer,
            old_amount_icp_e8s,
            new_balance_e8s,
        );
        if new_balance_e8s.saturating_sub(old_amount_icp_e8s) >= max_increment_e8s {
            log!(
                INFO,
                "Swap has reached the direct participation target of {} ICP e8s.",
                self.max_direct_participation_e8s(),
            );
        }

        Ok(RefreshBuyerTokensResponse {
```
