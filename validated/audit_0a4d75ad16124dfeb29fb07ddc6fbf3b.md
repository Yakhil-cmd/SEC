### Title
Missing Anonymous/Zero Principal Validation in SNS Swap `refresh_buyer_tokens` Allows Corrupted Buyer State and Potential Fund Loss - (File: `rs/sns/swap/canister/canister.rs`)

### Summary
The SNS Swap canister's `refresh_buyer_tokens` endpoint does not reject the anonymous principal (`2vxsx-fae`) as a buyer. An unprivileged caller can pass the anonymous principal string as the `buyer` field, causing a `BuyerState` entry to be created under the anonymous principal key. This corrupts the `buyers` map, inflates participation counters, and — because `sweep_icp` transfers funds to `principal.0` (the anonymous principal's default account) — causes ICP to be sent to the anonymous account rather than a real user, effectively burning those funds. The analog to the BeamNetwork bug is exact: a guard condition (buyer principal validity) does not apply to the zero/anonymous principal, leading to incorrect state, incorrect accounting, and potential fund loss.

### Finding Description
In `rs/sns/swap/canister/canister.rs`, the `refresh_buyer_tokens` handler resolves the buyer principal as follows:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    PrincipalId::from_str(&arg.buyer).unwrap()
};
``` [1](#0-0) 

If `arg.buyer` is set to the string `"2vxsx-fae"` (the anonymous principal), `PrincipalId::from_str` succeeds and the anonymous `PrincipalId` is passed directly to `refresh_buyer_token_e8s`. There is no check inside `refresh_buyer_token_e8s` that rejects the anonymous principal: [2](#0-1) 

The function proceeds to query the ICP ledger balance for the subaccount derived from the anonymous principal, and if the balance meets the minimum, it inserts a `BuyerState` entry keyed by `"2vxsx-fae"`: [3](#0-2) 

Later, during `sweep_icp`, the anonymous principal's buyer entry is iterated. The destination account is set to `Account { owner: principal.0, subaccount: None }` — i.e., the anonymous principal's default ICP account — and the ICP is transferred there: [4](#0-3) 

Tokens sent to the anonymous principal's default account are practically unrecoverable (no private key controls that account). Additionally, the anonymous principal entry inflates `self.buyers.len()`, which is used to enforce the `MAX_NEURONS_FOR_DIRECT_PARTICIPANTS` cap: [5](#0-4) 

This means a single anonymous-principal registration permanently consumes one participant slot, potentially blocking a legitimate participant from joining.

Furthermore, `error_refund_icp` does not validate the `source_principal_id` against the anonymous principal either: [6](#0-5) 

So repeated calls to `refresh_buyer_tokens` with the anonymous principal (each time with fresh ICP deposited to the anonymous subaccount) can repeatedly drain the swap's ICP escrow to the anonymous account.

### Impact Explanation
1. **Fund loss**: ICP swept to the anonymous principal's default account is unrecoverable. Any ICP deposited to the anonymous principal's subaccount of the swap canister and accepted via `refresh_buyer_tokens` will be transferred to `Account { owner: anonymous, subaccount: None }` during `sweep_icp`, where no one can retrieve it.
2. **Participant slot exhaustion**: The anonymous entry counts toward `MAX_NEURONS_FOR_DIRECT_PARTICIPANTS`, potentially blocking legitimate participants.
3. **Incorrect accounting**: `direct_participation_icp_e8s` and `buyer_total_icp_e8s` are inflated by the anonymous entry, causing `derived_state` queries to return incorrect values and potentially causing the swap to commit prematurely (if the anonymous ICP pushes total participation past the minimum threshold).
4. **Neuron recipe creation failure**: During `create_sns_neuron_recipes`, the anonymous principal is used to construct SNS neuron baskets. The `string_to_principal` call may succeed for the anonymous principal, producing invalid neuron recipes that count as `invalid` in the sweep result, permanently degrading finalization accounting.

### Likelihood Explanation
The attack is trivially reachable by any unprivileged ingress sender. The attacker only needs to:
1. Transfer a small amount of ICP (≥ `min_participant_icp_e8s`) to the anonymous principal's subaccount of the swap canister on the ICP ledger.
2. Call `refresh_buyer_tokens` with `buyer = "2vxsx-fae"`.

No privileged access, no governance majority, no threshold corruption is required. The ICP ledger freely accepts transfers to any subaccount of the swap canister. The swap canister's `refresh_buyer_tokens` is a public `#[update]` endpoint callable by anyone.

### Recommendation
Add an explicit check at the top of `refresh_buyer_token_e8s` (or in the canister handler before calling it) that rejects the anonymous principal:

```rust
if buyer == PrincipalId::new_anonymous() {
    return Err("Anonymous principal cannot participate in the swap".to_string());
}
``` [7](#0-6) 

Similarly, `error_refund_icp` should reject `source_principal_id == anonymous` to prevent any residual anonymous-subaccount balance from being swept to the uncontrolled anonymous account.

### Proof of Concept
1. Attacker calls ICP ledger `transfer` to send `min_participant_icp_e8s` ICP to `Account { owner: swap_canister_id, subaccount: Some(principal_to_subaccount(anonymous_principal)) }`.
2. Attacker calls `refresh_buyer_tokens({ buyer: "2vxsx-fae", confirmation_text: None })` on the swap canister.
3. The swap canister queries the ICP ledger balance for the anonymous subaccount, finds the deposited ICP, and inserts `BuyerState` for `"2vxsx-fae"` into `self.buyers`.
4. `self.update_total_participation_amounts()` is called, inflating `direct_participation_icp_e8s`.
5. When `sweep_icp` is called (during finalization), the anonymous buyer's ICP is transferred to `Account { owner: anonymous_principal, subaccount: None }` — an account with no controlling key — permanently losing those funds.
6. Steps 1–4 can be repeated (up to `MAX_NEURONS_FOR_DIRECT_PARTICIPANTS / neuron_basket_count` times) to exhaust participant slots and drain additional ICP to the anonymous account.

### Citations

**File:** rs/sns/swap/canister/canister.rs (L130-134)
```rust
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()
    };
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

**File:** rs/sns/swap/src/swap.rs (L1939-1948)
```rust
        let source_principal_id = match request {
            ErrorRefundIcpRequest {
                source_principal_id: Some(source_principal_id),
            } => source_principal_id,
            _ => {
                return ErrorRefundIcpResponse::new_invalid_request_error(format!(
                    "Invalid request. Must have source_principal_id. Request:\n{request:#?}",
                ));
            }
        };
```

**File:** rs/sns/swap/src/swap.rs (L2083-2094)
```rust
            let dst = if lifecycle == Lifecycle::Committed {
                // This Account should be given a name, such as SNS ICP Treasury...
                Account {
                    owner: sns_governance.get().0,
                    subaccount: None,
                }
            } else {
                Account {
                    owner: principal.0,
                    subaccount: None,
                }
            };
```
