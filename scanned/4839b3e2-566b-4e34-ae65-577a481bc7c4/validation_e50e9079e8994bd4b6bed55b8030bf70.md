### Title
Caller-Supplied `buyer` Parameter in `refresh_buyer_tokens` Bypasses Confirmation-Text Consent Gate — (`rs/sns/swap/canister/canister.rs`)

### Summary
The SNS Swap canister's `refresh_buyer_tokens` update method accepts a user-supplied `buyer` string field. When non-empty, the canister uses that string as the buyer's `PrincipalId` without verifying that the actual `ic_cdk::caller()` matches it. Any unprivileged ingress sender can therefore call `refresh_buyer_tokens` on behalf of any other principal, including providing the swap's required `confirmation_text` on that principal's behalf. This bypasses the explicit per-participant consent gate that the confirmation text is designed to enforce.

### Finding Description

In `rs/sns/swap/canister/canister.rs` the `refresh_buyer_tokens` handler resolves the effective buyer as follows:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    PrincipalId::from_str(&arg.buyer).unwrap()   // ← attacker-controlled
};
``` [1](#0-0) 

The resolved `p` is then passed directly to `refresh_buyer_token_e8s`, which:
1. Queries the ICP ledger for the balance of `p`'s subaccount on the swap canister.
2. Validates `confirmation_text` against the swap's required text.
3. Inserts or updates `p`'s `BuyerState` in `self.buyers`. [2](#0-1) 

The `confirmation_text` validation checks only that the supplied string matches the swap's publicly-visible initialization parameter; it does not verify that the caller is `p`. [3](#0-2) 

The `RefreshBuyerTokensRequest` proto explicitly documents this open-caller design:

> "If not specified, the caller is used." [4](#0-3) 

### Impact Explanation

**Confirmation-text consent bypass.** SNS swap creators can require participants to supply a specific acknowledgement string before their ICP is accepted. Because any caller can supply an arbitrary `buyer` principal together with the (publicly visible) confirmation text, the consent gate is trivially circumvented. A principal who deposited ICP into the swap subaccount but has not yet called `refresh_buyer_tokens` — perhaps because they are still reviewing the terms — can be force-registered as a consenting participant by a third party.

**Forced early swap commitment.** If enough principals have deposited ICP but are waiting before confirming, an attacker can call `refresh_buyer_tokens` for all of them in rapid succession, pushing the swap's total accepted ICP past the `max_direct_participation_icp_e8s` threshold and triggering an early commit. Once committed, participants cannot reclaim their ICP until finalization completes. [5](#0-4) 

### Likelihood Explanation

The attack requires no privileged access. Any ingress sender can submit an update call to the swap canister's `refresh_buyer_tokens` method with an arbitrary `buyer` string. The confirmation text is set at SNS initialization and is readable from the canister's public state. The only prerequisite is that the target principal has already transferred ICP to the swap subaccount — a condition that is also publicly observable on the ICP ledger.

### Recommendation

Enforce that the effective buyer equals the authenticated caller when a non-empty `buyer` field is supplied:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    let requested = PrincipalId::from_str(&arg.buyer).unwrap();
    if requested != caller_principal_id() {
        panic!("buyer field must match caller");
    }
    requested
};
```

If third-party notification is a required use-case (e.g., a relayer calling on behalf of a user), the confirmation text should be replaced with a caller-signed attestation, or the consent check should be separated from the balance-refresh step so that only the principal themselves can supply the confirmation.

### Proof of Concept

1. Alice transfers 10 ICP to the swap canister's subaccount derived from her principal, intending to review the confirmation text before committing.
2. Attacker reads the swap's `confirmation_text` from the canister's public init parameters.
3. Attacker submits:
   ```
   dfx canister call <swap_id> refresh_buyer_tokens \
     '(record { buyer = "<alice_principal>"; confirmation_text = opt "<public_text>" })'
   ```
4. The swap canister queries the ICP ledger, finds Alice's 10 ICP, validates the (attacker-supplied) confirmation text, and inserts Alice into `self.buyers` as a consenting participant — without Alice ever calling the method herself.
5. If this tips the swap past its ICP target, the swap auto-commits, locking all participants' ICP until finalization. [6](#0-5) [1](#0-0)

### Citations

**File:** rs/sns/swap/canister/canister.rs (L127-142)
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

**File:** rs/sns/swap/src/swap.rs (L1200-1215)
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
```

**File:** rs/sns/swap/src/swap.rs (L1285-1291)
```rust
        self.buyers
            .entry(buyer.to_string())
            .or_insert_with(|| BuyerState::new(0))
            .set_amount_icp_e8s(new_balance_e8s);
        // We compute the current participation amounts once and store the result in Swap's state,
        // for efficiency reasons.
        self.update_total_participation_amounts();
```

**File:** rs/sns/swap/src/gen/ic_sns_swap.pb.v1.rs (L1117-1126)
```rust
pub struct RefreshBuyerTokensRequest {
    /// If not specified, the caller is used.
    #[prost(string, tag = "1")]
    pub buyer: ::prost::alloc::string::String,
    /// To accept the swap participation confirmation, a participant should send
    /// the confirmation text via refresh_buyer_tokens, matching the text set
    /// during SNS initialization.
    #[prost(string, optional, tag = "2")]
    pub confirmation_text: ::core::option::Option<::prost::alloc::string::String>,
}
```
