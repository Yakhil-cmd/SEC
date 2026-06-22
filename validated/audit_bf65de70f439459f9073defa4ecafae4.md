### Title
Arbitrary `buyer` Parameter in `refresh_buyer_tokens` Allows Forced Swap Participation and Confirmation Text Bypass - (File: `rs/sns/swap/canister/canister.rs`)

### Summary

The SNS Swap canister's `refresh_buyer_tokens` update method accepts an arbitrary `buyer` principal string in the request body. When the field is non-empty, the canister uses that principal instead of `ic_cdk::caller()` to register swap participation. Any unprivileged ingress sender can call this method with any other user's principal ID, forcing that user's ICP (already sitting in their swap subaccount) into the swap and bypassing the explicit `confirmation_text` consent mechanism.

### Finding Description

In `rs/sns/swap/canister/canister.rs`, the `refresh_buyer_tokens` update handler reads the `buyer` field from the request body and, if non-empty, parses it as the acting principal instead of using the authenticated caller:

```rust
#[update]
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()   // ← arbitrary principal accepted
    };
    ...
    swap_mut()
        .refresh_buyer_token_e8s(p, arg.confirmation_text, this_canister_id(), &icp_ledger)
        .await
``` [1](#0-0) 

The `RefreshBuyerTokensRequest` protobuf explicitly documents this field as "If not specified, the caller is used": [2](#0-1) 

Inside `refresh_buyer_token_e8s`, the `buyer` principal is used to:
1. Look up the ICP balance of `buyer`'s subaccount on the swap canister
2. Validate the `confirmation_text` supplied by the **attacker** as if it were supplied by `buyer`
3. Register `buyer` as a swap participant and lock their ICP [3](#0-2) 

The `confirmation_text` validation compares the attacker-supplied text against the SNS-configured expected text — it does not verify that the text was submitted by the actual token holder: [4](#0-3) 

### Impact Explanation

**Confirmation text consent bypass**: SNS projects can require participants to explicitly agree to swap terms via `confirmation_text`. Because the attacker supplies this field on behalf of the victim, the victim is recorded as having consented to terms they never saw or agreed to. This undermines the only explicit consent mechanism in the swap participation flow.

**Forced participation / ICP lock-up**: A user who has transferred ICP to their swap subaccount but has not yet called `refresh_buyer_tokens` (e.g., they changed their mind) can be force-registered as a participant by an attacker. Their ICP becomes locked until the swap closes, at which point they must call `error_refund_icp` to recover it — if the swap commits, they receive SNS tokens they did not intend to purchase.

**Participant slot exhaustion**: An attacker can enumerate all principals with ICP in their swap subaccounts (observable on-chain via the ICP ledger) and call `refresh_buyer_tokens` for each, filling the `max_participants` cap and blocking legitimate new participants: [5](#0-4) 

### Likelihood Explanation

The attack requires no special privileges, no key material, and no trusted role. The attacker only needs to:
1. Observe the ICP ledger for transfers to the swap canister's subaccounts (fully public on-chain data)
2. Send a single ingress update call to `refresh_buyer_tokens` per victim with the victim's principal in the `buyer` field and the correct `confirmation_text` (which is public, stored in the swap's `Init` state and readable via `get_init`)

The swap's `get_init` query endpoint exposes the `confirmation_text` to anyone: [6](#0-5) 

This makes the attack fully automatable and permissionless.

### Recommendation

**Short term**: When the call arrives via ingress (i.e., from a user), always use `caller_principal_id()` and ignore the `buyer` field. If third-party notification is needed (e.g., for the Neurons' Fund canister-to-canister path), gate the `buyer` override on the caller being a known trusted canister (e.g., NNS governance).

**Long term**: Add an invariant test asserting that `refresh_buyer_tokens` called by principal A with `buyer = B` (where A ≠ B) is rejected when the call is an ingress message. Audit all other SNS Swap endpoints for similar caller-identity substitution patterns.

### Proof of Concept

```
// Attacker (Mallory) observes on the ICP ledger that Alice (alice_principal)
// has transferred ICP to her swap subaccount but has not yet called refresh_buyer_tokens.
//
// Mallory reads the confirmation_text from the public get_init endpoint.
// Mallory then sends:

dfx canister call <swap_canister_id> refresh_buyer_tokens '(
  record {
    buyer = "<alice_principal_text>";
    confirmation_text = opt "<confirmation_text_from_get_init>"
  }
)'

// Result: Alice is registered as a swap participant with her ICP locked,
// and the swap records that Alice agreed to the confirmation text —
// even though Alice never called this method herself.
```

The root cause is in `rs/sns/swap/canister/canister.rs` lines 130–134: the `buyer` field from the request body is used as the acting identity without any check that `arg.buyer == caller_principal_id()`. [7](#0-6)

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

**File:** rs/sns/swap/canister/canister.rs (L211-216)
```rust
/// Returns the initialization data of the canister
#[query]
async fn get_init(request: GetInitRequest) -> GetInitResponse {
    log!(INFO, "get_init");
    swap().get_init(&request)
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

**File:** rs/sns/swap/src/swap.rs (L1180-1197)
```rust
        {
            let num_direct_participants = self.buyers.len() as u64;
            let num_sns_neurons_per_basket = params
                .neuron_basket_construction_parameters
                .as_ref()
                .expect("neuron_basket_construction_parameters must be specified")
                .count;
            if (num_direct_participants + 1) * num_sns_neurons_per_basket
                > MAX_NEURONS_FOR_DIRECT_PARTICIPANTS
            {
                return Err(format!(
                    "The swap has reached the maximum number of direct participants ({num_direct_participants}) and does \
                     not accept new participants; existing participants may still increase their \
                     ICP participation amount. This constraint ensures that SNS neuron baskets can \
                     be created for all existing participants (SNS neuron basket size: {num_sns_neurons_per_basket}, \
                     MAX_NEURONS_FOR_DIRECT_PARTICIPANTS: {MAX_NEURONS_FOR_DIRECT_PARTICIPANTS}).",
                ));
            }
```
