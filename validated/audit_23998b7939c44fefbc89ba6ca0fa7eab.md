### Title
Unprivileged Caller Can Register Any Buyer's SNS Swap Participation, Bypassing Confirmation Text Consent Requirement - (File: `rs/sns/swap/canister/canister.rs`)

---

### Summary

The `refresh_buyer_tokens` endpoint in the SNS Swap canister accepts an arbitrary `buyer` principal in its request payload with no check that the caller is that buyer. Because the swap's `confirmation_text` is stored in publicly readable canister state, any unprivileged ingress sender can call `refresh_buyer_tokens` on behalf of any victim who has already transferred ICP to their swap subaccount, supplying the correct confirmation text and registering the victim's participation without their explicit consent. This is a direct analog of the Tracer M-13 finding: a missing caller-restriction on a sensitive state-mutating function allows a third party to act on behalf of another user, bypassing an intended access-control/consent mechanism.

---

### Finding Description

`refresh_buyer_tokens` in the SNS Swap canister is the function that finalizes a buyer's participation: it reads the buyer's ICP subaccount balance on the ICP ledger and records the participation amount in the swap's state. The canister endpoint resolves the buyer principal as follows:

```rust
// rs/sns/swap/canister/canister.rs  lines 128-143
#[update]
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()   // ← any caller can supply any principal
    };
    ...
    swap_mut()
        .refresh_buyer_token_e8s(p, arg.confirmation_text, ...)
        .await
``` [1](#0-0) 

There is **no check** that `caller == p`. The `confirmation_text` field is also taken directly from the caller's argument:

```rust
// rs/sns/swap/src/swap.rs  lines 1149-1150
self.validate_confirmation_text(confirmation_text)?;
``` [2](#0-1) 

`validate_confirmation_text` only checks that the supplied string matches the value stored in `Init.confirmation_text`; it does not verify that the caller is the buyer:

```rust
// rs/sns/swap/src/swap.rs  lines 363-384
pub fn validate_confirmation_text(
    &self,
    confirmation_text: Option<String>,
) -> Result<(), String> {
    match (
        self.init_or_panic().confirmation_text.as_ref(),
        confirmation_text,
    ) {
        (Some(expected_text), Some(text)) => {
            if &text != expected_text { Err(...) } else { Ok(()) }
        }
        ...
    }
}
``` [3](#0-2) 

The `confirmation_text` value is part of the `Init` struct, which is returned by the public `get_state` query endpoint, making it trivially readable by any observer:

```proto
// rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto  line 325
optional string confirmation_text = 15;
``` [4](#0-3) 

The `RefreshBuyerTokensRequest` proto explicitly documents that the `buyer` field is optional and defaults to the caller only if empty:

```proto
// rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto  lines 843-851
message RefreshBuyerTokensRequest {
  // If not specified, the caller is used.
  string buyer = 1;
  optional string confirmation_text = 2;
}
``` [5](#0-4) 

The integration test in `rs/tests/nns/sns/lib/src/sns_deployment.rs` explicitly demonstrates that the default identity (a third party) can successfully call `refresh_buyer_tokens` for the wealthy user after the ICP transfer, confirming this is reachable:

```rust
// rs/tests/nns/sns/lib/src/sns_deployment.rs  lines 919-926
let res_4 = {
    let request = sns_request_provider
        .refresh_buyer_tokens(Some(wealthy_user_identity.principal_id), None);
    block_on(default_sns_agent.call_and_parse(&request))
        .result()
        .unwrap()
};
``` [6](#0-5) 

---

### Impact Explanation

**1. Confirmation-text consent bypass (primary impact).**  
The `confirmation_text` mechanism exists so that participants must explicitly agree to swap terms before their ICP is committed. SNS projects use it for legal/compliance purposes (e.g., "I confirm I am not a US person"). Because the text is public and any caller can supply it on behalf of any buyer, an attacker can register a victim's participation without the victim ever having seen or agreed to the terms. The victim's ICP is then locked in the swap until it finalizes.

**2. Forced participation without user intent.**  
A user who transfers ICP to their swap subaccount to explore or test the swap (without having decided to participate) can be forced into the swap by an attacker calling `refresh_buyer_tokens` on their behalf. Once registered, the ICP is locked; the user cannot withdraw it until the swap commits or aborts.

**3. Participant-slot exhaustion griefing.**  
The swap enforces a hard cap `MAX_NEURONS_FOR_DIRECT_PARTICIPANTS` on the number of direct participants:

```rust
// rs/sns/swap/src/swap.rs  lines 1187-1196
if (num_direct_participants + 1) * num_sns_neurons_per_basket
    > MAX_NEURONS_FOR_DIRECT_PARTICIPANTS
{
    return Err(format!("The swap has reached the maximum number of direct participants..."));
}
``` [7](#0-6) 

An attacker who controls many accounts can transfer the minimum ICP to each, then call `refresh_buyer_tokens` for all of them to fill the participant cap, denying legitimate users from joining the swap. This is the direct analog of the Tracer griefing attack.

---

### Likelihood Explanation

- The `buyer` field is a plain text principal ID in the Candid interface, settable by any ingress caller.
- The `confirmation_text` is publicly readable from `get_state` with no authentication.
- No cycles cost or staking requirement gates the call.
- The ICP ledger is a public ledger; transfers to swap subaccounts are observable by anyone monitoring it.
- The attack requires only that the victim has already transferred ICP to their subaccount, which is a normal prerequisite step in the participation flow.
- The integration test suite explicitly exercises the third-party-caller path as a supported use case, confirming the path is reachable on mainnet.

---

### Recommendation

Add a caller-identity check inside `refresh_buyer_tokens` before resolving the buyer principal. When a non-empty `buyer` is supplied, verify that the caller matches the buyer:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    let requested = PrincipalId::from_str(&arg.buyer).unwrap();
    if requested != caller_principal_id() {
        panic!("Caller is not authorized to refresh tokens on behalf of {}", requested);
    }
    requested
};
```

Alternatively, if third-party notification is intentionally supported (e.g., for relayers), restrict it to a whitelisted set of principals (analogous to the whitelisted-relayer fix suggested in the Tracer report), and separately require that the `confirmation_text` can only be supplied by the buyer themselves, not by a third-party caller.

---

### Proof of Concept

1. Deploy an SNS swap with `confirmation_text = "I confirm I am eligible to participate"`.
2. Victim (`principal V`) transfers `min_participant_icp_e8s` ICP to the swap subaccount `swap_canister[principal_to_subaccount(V)]` on the ICP ledger, intending to review the terms before committing.
3. Attacker (`principal A`, any unprivileged identity) queries `get_state` on the swap canister to read `init.confirmation_text`.
4. Attacker submits an ingress update call to `refresh_buyer_tokens` with:
   ```
   RefreshBuyerTokensRequest {
     buyer: V.to_text(),
     confirmation_text: Some("I confirm I am eligible to participate"),
   }
   ```
5. The swap canister resolves `p = V`, queries the ICP ledger for V's subaccount balance (non-zero), validates the confirmation text (matches), and records V's participation in `self.buyers`.
6. V's ICP is now locked in the swap. V never explicitly confirmed the terms. If the swap commits, V receives SNS tokens they may not have wanted; if it aborts, V must wait for the swap to end and call `error_refund_icp` to recover funds.
7. By repeating steps 2–6 across many controlled accounts, the attacker can fill `MAX_NEURONS_FOR_DIRECT_PARTICIPANTS` slots, blocking all remaining legitimate participants. [1](#0-0) [8](#0-7) [3](#0-2)

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

**File:** rs/sns/swap/src/swap.rs (L1187-1196)
```rust
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
```

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L322-326)
```text
  // An optional text that swap participants should confirm before they may
  // participate in the swap. If the field is set, its value should be plain
  // text with at least 1 and at most 1,000 characters.
  optional string confirmation_text = 15;

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

**File:** rs/tests/nns/sns/lib/src/sns_deployment.rs (L919-926)
```rust
    // Use the default identity to call refresh_buyer_tokens for the wealthy user
    let res_4 = {
        let request = sns_request_provider
            .refresh_buyer_tokens(Some(wealthy_user_identity.principal_id), None);
        block_on(default_sns_agent.call_and_parse(&request))
            .result()
            .unwrap()
    };
```
