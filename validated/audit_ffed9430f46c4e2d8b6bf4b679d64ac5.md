Audit Report

## Title
Unprivileged Caller Can Register Arbitrary Principal's ICP Participation and Bypass Confirmation Text Consent in SNS Swap - (`rs/sns/swap/canister/canister.rs`)

## Summary
The `refresh_buyer_tokens` endpoint accepts a caller-supplied `buyer` field and uses it as the participating principal with no check that the caller is that principal. Any unprivileged caller can register another principal's already-deposited ICP as swap participation, including supplying the SNS-configured confirmation text on their behalf. This bypasses the confirmation text consent mechanism and forces participation registration without the victim's explicit action.

## Finding Description
In `rs/sns/swap/canister/canister.rs` at lines 126–143, the `refresh_buyer_tokens` update method resolves the participating principal from the caller-supplied `arg.buyer` string with no authorization check:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    PrincipalId::from_str(&arg.buyer).unwrap()  // attacker-controlled
};
``` [1](#0-0) 

The proto definition at `rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto` lines 843–851 documents the `buyer` field as "If not specified, the caller is used," with no restriction on who may supply a non-empty value. [2](#0-1) 

The resolved principal is passed directly into `refresh_buyer_token_e8s` in `rs/sns/swap/src/swap.rs`, which queries the ICP ledger balance of `subaccount(swap_canister, buyer)` and records it as the buyer's participation: [3](#0-2) 

The `confirmation_text` field is also passed through from the attacker's request. `validate_confirmation_text` only checks that the supplied text matches the SNS-configured text — it does not verify that the caller is the buyer: [4](#0-3) 

The confirmation text is set at SNS initialization and is publicly readable. Any attacker who knows it (trivially, since it is public) can supply it on behalf of any victim, satisfying the consent gate without the victim's involvement. The buyer state is then written under the victim's principal: [5](#0-4) 

No existing check in the canister handler or in `refresh_buyer_token_e8s` verifies that `caller == buyer`. The only guards present are lifecycle state checks and the confirmation text string comparison, neither of which prevents third-party invocation.

## Impact Explanation
This matches the **High** bounty impact: "Significant SNS security impact with concrete user or protocol harm."

1. **Confirmation text consent bypass**: The confirmation text is an explicit legal/consent gate requiring the buyer to agree to swap terms. An attacker can satisfy it on behalf of any victim whose ICP is already in the swap subaccount, rendering the mechanism entirely ineffective. The victim's participation is recorded as having "accepted" terms they never read.

2. **Forced participation registration without consent**: Once a victim transfers ICP to their swap subaccount (a normal preparatory step), an attacker can immediately register that ICP as committed participation. The victim cannot directly withdraw from the subaccount — recovery requires `error_refund_icp`, which is only available after the swap closes. If the swap commits, the victim receives SNS tokens they did not choose to acquire at that time.

3. **Swap outcome manipulation**: An attacker can call `refresh_buyer_tokens` for many principals that have ICP in their subaccounts, forcing them to participate. This can push the swap past the minimum-participants threshold (causing it to commit when it would otherwise abort) or exhaust `MAX_NEURONS_FOR_DIRECT_PARTICIPANTS`, blocking legitimate new participants. [6](#0-5) 

## Likelihood Explanation
- No privilege is required; any unprivileged ingress sender can call `refresh_buyer_tokens` with an arbitrary `buyer` field.
- The only precondition is that the victim has transferred ICP to `subaccount(swap_canister, victim_principal)` — a normal, observable on-chain action.
- The confirmation text is publicly visible in SNS initialization parameters; no secret knowledge is required.
- The attack is a single ingress update call and is trivially automatable across many victims simultaneously.

## Recommendation
**Short term**: In `refresh_buyer_tokens` (`rs/sns/swap/canister/canister.rs`), when `arg.buyer` is non-empty, assert that the parsed principal equals `caller_principal_id()`. Only the buyer themselves should be permitted to register their own participation and supply the confirmation text.

**Long term**: Treat `refresh_buyer_tokens` as a caller-authenticated operation. Remove the ability to specify an arbitrary `buyer` principal entirely, or introduce an explicit delegation/allowance mechanism (analogous to ICRC-2 `approve`/`transfer_from`) if third-party notification is a desired feature. The confirmation text validation must be tied to the authenticated caller, not to the caller-supplied buyer field.

## Proof of Concept
**Setup**:
- SNS swap is in `Open` lifecycle with confirmation text `"I agree to participate"`.
- Alice (`alice_principal`) transfers 10 ICP to `Account { owner: swap_canister, subaccount: principal_to_subaccount(alice_principal) }` on the ICP ledger.
- Alice has not yet called `refresh_buyer_tokens`.

**Attack** (Eve, any unprivileged principal, using the state machine test harness as a template from `rs/sns/test_utils/src/state_test_helpers.rs` lines 291–309): [7](#0-6) 

```rust
state_machine.execute_ingress_as(
    eve_principal,          // Eve is the caller
    swap_canister_id,
    "refresh_buyer_tokens",
    Encode!(&RefreshBuyerTokensRequest {
        buyer: alice_principal.to_string(),   // victim's principal
        confirmation_text: Some("I agree to participate".to_string()),
    }).unwrap(),
).unwrap();
```

**Expected result**:
- The swap canister queries the ICP ledger balance of Alice's subaccount, finds 10 ICP.
- `swap.buyers["alice"] = BuyerState { amount_icp_e8s: 10_0000_0000 }` is written.
- The confirmation text is marked as accepted for Alice — without Alice ever having called the endpoint.
- If the swap commits, Alice receives SNS tokens she did not choose to acquire at this time; if it aborts, she recovers ICP minus fees, but her funds were locked and her consent was forged.

### Citations

**File:** rs/sns/swap/canister/canister.rs (L130-134)
```rust
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()
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

**File:** rs/sns/swap/src/swap.rs (L1285-1288)
```rust
        self.buyers
            .entry(buyer.to_string())
            .or_insert_with(|| BuyerState::new(0))
            .set_amount_icp_e8s(new_balance_e8s);
```

**File:** rs/sns/test_utils/src/state_test_helpers.rs (L291-309)
```rust
    let response = state_machine
        .execute_ingress(
            swap_canister_id,
            "refresh_buyer_tokens",
            Encode!(&RefreshBuyerTokensRequest {
                buyer: participant_principal_id.to_string(),
                confirmation_text: None,
            })
            .unwrap(),
        )
        .unwrap();
    let response = match response {
        WasmResult::Reply(reply) => reply,
        WasmResult::Reject(reject) => {
            panic!("refresh_buyer_tokens was rejected by the swap canister: {reject:#?}")
        }
    };

    Decode!(&response, RefreshBuyerTokensResponse).unwrap()
```
