### Title
Arbitrary `buyer` Field in `refresh_buyer_tokens` Allows Forced Participation and Confirmation-Text Consent Bypass — (File: `rs/sns/swap/canister/canister.rs`)

---

### Summary

The SNS Swap canister's `refresh_buyer_tokens` update endpoint accepts an arbitrary `buyer` principal supplied by the caller in the request payload. No check is performed to verify that the supplied `buyer` matches the actual ingress sender. Any unprivileged principal can therefore register any other principal as a swap participant — including accepting the swap's mandatory `confirmation_text` on that principal's behalf — without that principal's knowledge or consent.

---

### Finding Description

In `rs/sns/swap/canister/canister.rs`, the handler resolves the effective buyer from the caller-controlled payload field rather than from the authenticated ingress sender:

```rust
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()   // ← arbitrary, caller-controlled
    };
    ...
    swap_mut()
        .refresh_buyer_token_e8s(p, arg.confirmation_text, this_canister_id(), &icp_ledger)
        .await
}
``` [1](#0-0) 

The `RefreshBuyerTokensRequest.buyer` field is documented as "If not specified, the caller is used": [2](#0-1) 

Inside `refresh_buyer_token_e8s`, the swap canister:

1. Queries the ICP ledger balance of `Account { owner: swap_canister_id, subaccount: principal_to_subaccount(&buyer) }` — where `buyer` is the attacker-supplied value, not the caller.
2. If the balance meets `min_participant_icp_e8s`, registers `buyer` as a participant.
3. Validates `confirmation_text` against the swap's configured text and records acceptance — attributed to `buyer`, not to the actual caller. [3](#0-2) 

The `confirmation_text` field is the SNS's explicit informed-consent gate: [4](#0-3) 

Because both the `buyer` identity and the `confirmation_text` acceptance are resolved from the same caller-controlled message, an attacker can:

**Attack path A — zero-cost forced registration (no attacker ICP required):**
1. Victim `Bob` transfers `min_participant_icp_e8s` ICP to the swap's subaccount derived from his own principal (the normal first step of participation).
2. Before Bob calls `refresh_buyer_tokens` himself, attacker `Alice` calls it with `buyer = Bob` and `confirmation_text = <swap's required text>`.
3. Bob is registered as a participant; his ICP is locked; the swap records that Bob accepted the confirmation text — without Bob ever sending a message to the swap canister.

**Attack path B — attacker-funded forced registration:**
1. Alice transfers `min_participant_icp_e8s` ICP to the swap's subaccount for any target principal `T` (a canister, a governance neuron controller, etc.).
2. Alice calls `refresh_buyer_tokens` with `buyer = T` and `confirmation_text = <swap's required text>`.
3. `T` is registered as a participant and recorded as having consented.

The `buyers` map is keyed by the supplied `buyer` string, and `min_participation_reached` counts `self.buyers.len()`: [5](#0-4) 

This means an attacker who controls many cheap principals can artificially inflate the participant count, potentially triggering early commitment of the swap (once `max_direct_participation_icp_e8s` is also reached) and locking out legitimate participants who arrive after the cap is hit.

---

### Impact Explanation

1. **Consent bypass**: The `confirmation_text` mechanism exists to ensure each participant has read and agreed to the SNS's terms before their ICP is committed. Any caller can satisfy this check on behalf of any other principal, rendering the consent gate ineffective.

2. **Forced participation**: A victim who has pre-funded their swap subaccount (the normal flow) can be registered as a participant — with their ICP locked — before they choose to participate, and without them ever sending a message to the swap canister.

3. **Swap-outcome manipulation**: By registering many attacker-controlled principals as participants (each funded with `min_participant_icp_e8s`), an attacker can push the swap to `max_direct_participation_icp_e8s` early, triggering immediate commitment and excluding legitimate participants who arrive later. The attacker's ICP is swept to the SNS governance canister on finalization, so the cost is real but the attacker receives SNS neurons in return.

---

### Likelihood Explanation

The `refresh_buyer_tokens` endpoint is a public, unauthenticated update call reachable by any ingress sender. No role, key, or privileged access is required. Attack path A costs the attacker nothing beyond a single update call; it only requires that the victim has already transferred ICP to their subaccount, which is the standard first step of participation. Attack path B requires the attacker to spend `min_participant_icp_e8s` ICP per target, but the attacker receives SNS neurons in return, making the net cost only the opportunity cost of locked ICP.

---

### Recommendation

Enforce that the effective buyer is always the authenticated ingress sender, or — if third-party notification is intentionally supported — prohibit the caller from supplying `confirmation_text` on behalf of a different principal. Concretely:

```rust
if !arg.buyer.is_empty() {
    let specified = PrincipalId::from_str(&arg.buyer).unwrap();
    if specified != caller_principal_id() && !arg.confirmation_text.is_none() {
        ic_cdk::trap("confirmation_text may not be submitted on behalf of another principal");
    }
}
```

Alternatively, require that `buyer`, when non-empty, must equal the caller, matching the pattern used by other IC canisters that explicitly verify caller-vs-beneficiary identity.

---

### Proof of Concept

```
// Victim Bob transfers 2 ICP to the swap's subaccount for his own principal.
// (Normal first step; Bob has not yet called refresh_buyer_tokens.)

// Attacker Alice sends the following ingress message:
refresh_buyer_tokens({
    buyer: "<Bob's principal text>",
    confirmation_text: Some("I confirm I have read the terms.")
})

// Result:
// - swap.buyers["<Bob>"] is created with amount = 2 ICP
// - swap records Bob as having accepted the confirmation text
// - Bob's ICP is now locked; Bob never sent a message to the swap canister
// - If this was the last ICP needed to reach max_direct_participation_icp_e8s,
//   the swap commits immediately, excluding all subsequent legitimate participants
```

Relevant code locations: [1](#0-0) [6](#0-5) [7](#0-6)

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

**File:** rs/sns/swap/src/swap.rs (L1122-1127)
```rust
    ///
    /// If the SNS had specified a swap confirmation text, the caller of this
    /// function must accept this confirmation by sending the exact same text
    /// as an argument to this function (otherwise, the call will result in
    /// an error).
    ///
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

**File:** rs/sns/swap/src/swap.rs (L2801-2821)
```rust
    pub fn min_participation_reached(&self) -> bool {
        if let (Some(params), Some(init)) = (&self.params, &self.init) {
            if init.neurons_fund_participation.is_some() {
                // Only count direct participants for determining swap's success.
                // Note that a valid Swap Init should either have `neurons_fund_participation` or
                // `cf_participants`, but not both at the same time; here, we defensively perform
                // the check again anyway.
                if !self.cf_participants.is_empty() {
                    log!(
                        ERROR,
                        "Inconsistent Swap Init: cf_participants has {} elements (starting with \
                        {:?}) while neurons_fund_participation is set.",
                        self.cf_participants.len(),
                        self.cf_participants[0],
                    );
                }
                (self.buyers.len() as u32) >= params.min_participants
            } else {
                (self.cf_participants.len().saturating_add(self.buyers.len()) as u32)
                    >= params.min_participants
            }
```
