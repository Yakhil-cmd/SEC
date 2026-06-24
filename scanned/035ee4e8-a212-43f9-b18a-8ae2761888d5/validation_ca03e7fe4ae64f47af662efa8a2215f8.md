### Title
SNS Developer Team Can Self-Deal in Their Own Swap to Inflate the Clearing Price — (File: `rs/sns/swap/src/swap.rs`)

---

### Summary

The SNS swap canister implements a single-price auction where the clearing price is `sns_tokens / total_ICP`. The `refresh_buyer_token_e8s` function, which registers buyer participation, performs **no check** preventing the SNS developer team (identified by `fallback_controller_principal_ids`) from participating as buyers in their own swap. Combined with the fully public, unauthenticated participation state exposed via `get_state` and `list_direct_participants`, the developer team can monitor all contributions in real-time and strategically inflate the total ICP pool to increase the price per SNS token, causing other participants to receive fewer tokens per ICP than they would otherwise.

---

### Finding Description

The SNS swap canister is a single-price auction: the effective token price is `sns_tokens / total_ICP_contributed`. All participation data is publicly readable by anyone via unauthenticated query calls:

- `get_state` returns the full `Swap` struct including the entire `buyers` map (all principals and their ICP amounts).
- `list_direct_participants` paginates over all buyers with their `BuyerState`.
- `get_buyer_state` returns any individual buyer's committed amount. [1](#0-0) [2](#0-1) 

The `refresh_buyer_token_e8s` function validates:
1. Lifecycle is OPEN
2. Direct participation ceiling not reached
3. Confirmation text matches (if set)
4. Amount ≥ `min_participant_icp_e8s`
5. Amount ≤ `max_participant_icp_e8s`
6. Max participant count not exceeded [3](#0-2) 

There is **no check** against the `fallback_controller_principal_ids` (the SNS developer team — the "seller" equivalent). The `Init` struct stores these principals: [4](#0-3) 

but `refresh_buyer_token_e8s` never consults them. Any principal, including those listed as fallback controllers, can call `refresh_buyer_tokens` and register ICP participation. [5](#0-4) 

The attack path:

1. Developer creates an SNS, listing themselves in `fallback_controller_principal_ids`.
2. The swap opens (`LIFECYCLE_OPEN`).
3. Developer polls `get_state` (public, unauthenticated) to observe all current buyer contributions and the running total ICP.
4. Developer calls `refresh_buyer_tokens` from any principal (including their own) to contribute ICP.
5. Total ICP increases → `sns_tokens / total_ICP` decreases → each ICP buys fewer SNS tokens → all other participants receive fewer tokens per ICP than they would have without the developer's participation.
6. If the developer pushes total ICP to `max_direct_participation_icp_e8s`, the swap commits immediately, cutting off participants who had not yet contributed. [6](#0-5) 

The contributed ICP flows to the SNS governance canister (the SNS treasury), which the developer team controls via their pre-allocated developer neurons. The developer team receives SNS neurons in return. [7](#0-6) 

---

### Impact Explanation

Other participants receive fewer SNS tokens per ICP than they would have without the developer's participation. In the extreme case, the developer team can push total ICP to `max_direct_participation_icp_e8s`, triggering immediate commitment and preventing further participation from legitimate buyers. The developer team's contributed ICP goes to the SNS treasury they control, and they receive SNS neurons in return — making the net cost of the attack potentially recoverable. Participants who joined expecting a certain token allocation are harmed financially. [8](#0-7) 

---

### Likelihood Explanation

Medium. The developer team must commit real ICP to execute the attack, but the ICP flows to the SNS treasury they govern, and they receive SNS tokens in return. The attack requires no special technical capability beyond calling a public canister endpoint. The fully public participation state (`get_state`, `list_direct_participants`) means the developer can time their contribution precisely for maximum effect. Any SNS launch is a candidate for this manipulation. [9](#0-8) 

---

### Recommendation

Add a validation step in `refresh_buyer_token_e8s` that rejects participation from any principal listed in `init.fallback_controller_principal_ids`. Since these principals are the "seller" equivalent — the team that created the SNS and stands to benefit from a higher clearing price — they should be excluded from direct participation. Alternatively, if self-participation is intentionally permitted, it must be prominently disclosed to swap participants so they can price in the risk. [10](#0-9) 

---

### Proof of Concept

```
// Developer observes current state (unauthenticated query, anyone can call)
let state = swap_canister.get_state({});
// state.swap.buyers contains all principals and their ICP amounts
// state.derived.direct_participation_icp_e8s shows running total

// Developer participates from their own principal (no restriction)
swap_canister.refresh_buyer_tokens({
    buyer: developer_principal.to_string(),
    confirmation_text: None,
});
// Total ICP increases → price per SNS token increases
// Other participants receive fewer SNS tokens per ICP
// If total reaches max_direct_participation_icp_e8s, swap commits immediately
``` [11](#0-10) [12](#0-11)

### Citations

**File:** rs/sns/swap/canister/canister.rs (L97-109)
```rust
#[query]
fn get_state(_arg: GetStateRequest) -> GetStateResponse {
    swap().get_state()
}

/// Get the state of a buyer. This will return a `GetBuyerStateResponse`
/// with an optional `BuyerState` struct if the Swap Canister has
/// been successfully notified of a buyer's ICP transfer.
#[query]
fn get_buyer_state(request: GetBuyerStateRequest) -> GetBuyerStateResponse {
    log!(INFO, "get_buyer_state");
    swap().get_buyer_state(&request)
}
```

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

**File:** rs/sns/swap/canister/canister.rs (L237-244)
```rust
/// Lists direct participants in the Swap.
#[query]
async fn list_direct_participants(
    request: ListDirectParticipantsRequest,
) -> ListDirectParticipantsResponse {
    log!(INFO, "list_direct_participants");
    swap().list_direct_participants(request)
}
```

**File:** rs/sns/swap/src/swap.rs (L1134-1200)
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

        // Recheck lifecycle state and ICP target after async call because the swap could have
        // been closed (committed or aborted) while the call to get the account balance was
        // outstanding.
        self.validate_lifecycle_is_open()
            .map_err(context_after_awaiting_icp_ledger_response)?;
        self.validate_possibility_of_direct_participation()
            .map_err(context_after_awaiting_icp_ledger_response)?;

        // Once swap is OPEN, the Swap.params field is set. In light of validation performed
        // above, we should be able to `expect` this value without a panic.
        let params = &self.params.as_ref().expect("Expected params to be set");

        let max_increment_e8s = self.available_direct_participation_e8s();

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
        }

        // Check that the minimum amount has been transferred before
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

**File:** rs/sns/swap/src/swap.rs (L1556-1562)
```rust
        // Transfer the ICP tokens from the Swap canister.
        finalize_swap_response
            .set_sweep_icp_result(self.sweep_icp(now_fn, environment.icp_ledger()).await);
        if finalize_swap_response.has_error_message() {
            return finalize_swap_response;
        }

```

**File:** rs/sns/swap/src/gen/ic_sns_swap.pb.v1.rs (L278-282)
```rust
    /// If the swap is aborted, control of the canister(s) should be set to these
    /// principals. Must not be empty.
    #[prost(string, repeated, tag = "11")]
    pub fallback_controller_principal_ids: ::prost::alloc::vec::Vec<::prost::alloc::string::String>,
    /// Same as SNS ledger. Must hold the same value as SNS ledger. Whether the
```

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L133-140)
```text
// Step 3a. (State COMMITTED). Tokens are allocated to participants at
// a single clearing price, i.e., the number of SNS tokens being
// offered divided by the total number of ICP tokens contributed to
// the swap. In this state, a call to `finalize` will create SNS
// neurons for each participant and transfer ICP to the SNS governance
// canister. The call to `finalize` does not happen automatically
// (i.e., on the canister heartbeat) so that there is a caller to
// respond to with potential errors.
```

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L783-788)
```text
// TODO: introduce a limits on the number of buyers to include?
message GetStateRequest {}
message GetStateResponse {
  Swap swap = 1;
  DerivedState derived = 2;
}
```
