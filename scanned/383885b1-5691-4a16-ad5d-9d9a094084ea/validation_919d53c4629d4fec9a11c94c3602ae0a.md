### Title
Caller-Supplied `buyer` Principal in `refresh_buyer_tokens` Bypasses Confirmation-Text Enforcement on Behalf of Victim - (`rs/sns/swap/canister/canister.rs`)

### Summary

The SNS Swap canister's `refresh_buyer_tokens` update method accepts a caller-supplied `buyer` principal string. When the `buyer` field is non-empty, the canister uses that arbitrary principal instead of `msg_caller()`. The `confirmation_text` is validated against the *caller's* supplied text, but the ICP balance is read from the *victim's* subaccount and the participation is credited to the *victim's* buyer state. An unprivileged ingress sender can therefore force any principal who has already transferred ICP into their swap subaccount to be registered as a participant — including bypassing the confirmation-text gate on their behalf — without the victim ever sending the confirmation text themselves.

### Finding Description

In `rs/sns/swap/canister/canister.rs`, the `refresh_buyer_tokens` endpoint resolves the buyer principal as follows:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    PrincipalId::from_str(&arg.buyer).unwrap()   // ← arbitrary caller-supplied value
};
``` [1](#0-0) 

This `p` is then passed directly into `refresh_buyer_token_e8s`:

```rust
swap_mut()
    .refresh_buyer_token_e8s(p, arg.confirmation_text, this_canister_id(), &icp_ledger)
    .await
``` [2](#0-1) 

Inside `refresh_buyer_token_e8s`, the `confirmation_text` is validated once (before the async ledger call), but it is validated against the *caller's* supplied text — not against any text the victim principal ever provided:

```rust
self.validate_confirmation_text(confirmation_text)?;
``` [3](#0-2) 

The ICP balance is then read from the subaccount derived from the *attacker-supplied* `buyer` principal:

```rust
let account = Account {
    owner: this_canister.get().0,
    subaccount: Some(principal_to_subaccount(&buyer)),
};
icp_ledger.account_balance(account).await ...
``` [4](#0-3) 

And the resulting participation is recorded under that same `buyer` key:

```rust
self.buyers
    .entry(buyer.to_string())
    .or_insert_with(|| BuyerState::new(0))
    .set_amount_icp_e8s(new_balance_e8s);
``` [5](#0-4) 

The `RefreshBuyerTokensRequest` proto explicitly documents this design:

```proto
message RefreshBuyerTokensRequest {
  // If not specified, the caller is used.
  string buyer = 1;
  ...
}
``` [6](#0-5) 

### Impact Explanation

**Confirmation-text bypass:** When an SNS configures a `confirmation_text` (a legal/compliance gate), the intent is that only a principal who explicitly sends the matching text can be registered as a participant. Because the attacker supplies both the `buyer` principal and the `confirmation_text`, the attacker can satisfy the text check on behalf of any victim who has already deposited ICP into their swap subaccount. The victim is registered as a participant without ever having sent the confirmation text, violating the legal/compliance invariant the SNS operator intended to enforce.

**Forced participation:** Any principal that has pre-funded their swap subaccount (e.g., via a ticket-based flow or a direct transfer) can be forcibly committed to the swap by a third party before the victim decides to participate. Once committed, the ICP is locked until the swap finalizes (committed or aborted). This is the direct IC analog of the `PerpDepository#rebalance` pattern: an unpermissioned function uses a caller-supplied account as the economic subject of a state-changing operation.

**Participant-slot exhaustion:** An attacker can enumerate victim principals who have funded subaccounts and call `refresh_buyer_tokens` for each, consuming all available participant slots (`MAX_NEURONS_FOR_DIRECT_PARTICIPANTS`) with victims, preventing legitimate new participants from joining. [7](#0-6) 

### Likelihood Explanation

- `refresh_buyer_tokens` is a public, unpermissioned `#[update]` endpoint callable by any ingress sender with no cycles or token cost.
- The `buyer` field is documented as optional ("if not specified, the caller is used"), making it an intentional feature that is trivially abused.
- Any principal who uses the ticket-based payment flow or directly transfers ICP to their swap subaccount before calling `refresh_buyer_tokens` themselves is vulnerable during that window.
- No special privileges, key material, or majority corruption is required.

### Recommendation

1. **Enforce `buyer == caller`**: When `arg.buyer` is non-empty, verify it equals `caller_principal_id()` before proceeding, or remove the `buyer` override entirely and always use `caller_principal_id()`.

```rust
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    let p: PrincipalId = caller_principal_id(); // always use caller
    // ignore arg.buyer
    ...
}
```

2. If third-party notification is a required use-case (e.g., a bot notifying on behalf of a user), add an explicit allowlist or require the victim to have pre-authorized the caller via a separate on-chain approval.

### Proof of Concept

1. Alice transfers 10 ICP to `swap_canister_subaccount(alice_principal)` on the ICP ledger.
2. The SNS has `confirmation_text = "I agree to the terms"`. Alice has not yet called `refresh_buyer_tokens`.
3. Mallory (any principal) calls:
   ```
   refresh_buyer_tokens({
     buyer: alice_principal.to_string(),
     confirmation_text: Some("I agree to the terms")
   })
   ```
4. The swap canister resolves `p = alice_principal`, reads Alice's subaccount balance (10 ICP), validates Mallory's confirmation text as if it were Alice's, and records Alice as a committed participant.
5. Alice's ICP is now locked in the swap. Alice never sent the confirmation text. The SNS operator's compliance gate has been bypassed. [8](#0-7) [9](#0-8)

### Citations

**File:** rs/sns/swap/canister/canister.rs (L128-143)
```rust
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

**File:** rs/sns/swap/src/swap.rs (L1179-1197)
```rust
        // Check that the maximum number of participants has not been reached yet.
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
