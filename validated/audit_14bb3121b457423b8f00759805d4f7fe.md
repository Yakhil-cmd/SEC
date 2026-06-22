### Title
Unauthorized Third-Party Griefing via `refresh_buyer_tokens` Caller/Buyer Mismatch — (`rs/sns/swap/canister/canister.rs`)

### Summary
The SNS Swap canister's `refresh_buyer_tokens` endpoint accepts an arbitrary `buyer` principal in the request body without verifying it matches the actual caller. Any unprivileged ingress sender can call this endpoint on behalf of any buyer who has already transferred ICP to the swap subaccount, locking that buyer's funds into the swap and — when a swap `confirmation_text` is configured — forging the buyer's acceptance of the swap's terms of service.

### Finding Description

The `refresh_buyer_tokens` update method in `rs/sns/swap/canister/canister.rs` resolves the effective buyer principal from the caller-supplied `arg.buyer` field rather than from the authenticated ingress sender:

```rust
#[update]
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()   // ← any principal accepted
    };
    ...
    swap_mut()
        .refresh_buyer_token_e8s(p, arg.confirmation_text, this_canister_id(), &icp_ledger)
        .await
``` [1](#0-0) 

The inner `refresh_buyer_token_e8s` function then:
1. Reads the ICP ledger balance of `buyer`'s subaccount on the swap canister.
2. Validates the caller-supplied `confirmation_text` against the swap's required confirmation text (if any).
3. Permanently records the buyer's participation in `self.buyers`. [2](#0-1) 

The `confirmation_text` field is validated only for content equality against the swap-configured text — it is not tied to the authenticated caller: [3](#0-2) 

The `RefreshBuyerTokensRequest` proto explicitly documents the `buyer` field as optional (defaulting to caller), but imposes no restriction on who may supply it: [4](#0-3) 

### Impact Explanation

**Forced participation / term-acceptance forgery.** When a swap is configured with a `confirmation_text` (a legal/terms-of-service acceptance gate), an attacker who knows the text (it is publicly readable from swap state) can call `refresh_buyer_tokens` with any victim buyer's principal and the correct confirmation text. This:

- Permanently records the victim as having accepted the swap's terms without their knowledge or consent.
- Locks the victim's already-transferred ICP into the swap's accepted participation ledger, removing their ability to reclaim it via `error_refund_icp` after the swap closes.

**Griefing without confirmation text.** Even without a `confirmation_text`, any attacker can force-register a buyer's participation the moment ICP appears in the buyer's swap subaccount. A buyer who transferred ICP by mistake, or who changed their mind before calling `refresh_buyer_tokens`, loses the ability to recover funds via `error_refund_icp` once an attacker triggers the registration. [5](#0-4) 

### Likelihood Explanation

- The endpoint is publicly reachable by any unprivileged ingress sender with no preconditions beyond the victim having a non-zero ICP balance in their swap subaccount.
- The `confirmation_text` is readable from public swap state, so no secret knowledge is required.
- The attack window is the entire `Open` lifecycle of the swap — potentially days to weeks.
- A motivated attacker (e.g., a competing SNS participant, or someone who wants to force a swap to commit by pushing borderline participants over the minimum) has clear incentive.

### Recommendation

Enforce that the resolved buyer principal equals the authenticated caller when a non-empty `buyer` field is supplied:

```rust
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    let caller = caller_principal_id();
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller
    } else {
        let requested = PrincipalId::from_str(&arg.buyer).unwrap();
        if requested != caller {
            panic!("buyer field must match the caller");
        }
        requested
    };
    ...
}
```

If third-party notification (calling on behalf of another buyer) is intentionally desired, it must be restricted to cases where no `confirmation_text` is required, since accepting terms on behalf of another principal is never safe.

### Proof of Concept

1. SNS swap is deployed with `confirmation_text = "I agree to the SNS terms"` and is in `Open` state.
2. Victim Alice transfers 10 ICP to `swap_canister_subaccount(alice_principal)` on the ICP ledger but has not yet called `refresh_buyer_tokens`.
3. Attacker Eve (any principal) submits an ingress call:
   ```
   refresh_buyer_tokens({
     buyer: alice_principal.to_text(),
     confirmation_text: Some("I agree to the SNS terms")
   })
   ```
4. The swap canister resolves `p = alice_principal`, reads Alice's 10 ICP balance, validates the confirmation text (which matches), and records Alice as a committed participant.
5. Alice's ICP is now locked. `error_refund_icp` will return 0 for Alice after the swap closes because her ICP was accepted. Alice never consented to the terms. [6](#0-5) [7](#0-6)

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

**File:** rs/sns/swap/src/swap.rs (L1200-1225)
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

        if old_amount_icp_e8s >= e8s {
            // Already up-to-date. Strict inequality can happen if messages are re-ordered.
            return Ok(RefreshBuyerTokensResponse {
                icp_accepted_participation_e8s: old_amount_icp_e8s,
                icp_ledger_account_balance_e8s: e8s,
            });
        }
        // Subtraction safe because of the preceding if-statement.
        let requested_increment_e8s = e8s - old_amount_icp_e8s;
        let actual_increment_e8s = std::cmp::min(max_increment_e8s, requested_increment_e8s);
        let new_balance_e8s = old_amount_icp_e8s.saturating_add(actual_increment_e8s);
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
