### Title
SNS Swap Participant Slot Exhaustion via Sybil Participation — (`rs/sns/swap/src/swap.rs`)

---

### Summary

The SNS Swap canister enforces a hard cap on the number of direct participants via `MAX_NEURONS_FOR_DIRECT_PARTICIPANTS`. Once a principal is added to the `buyers` map, there is no mechanism to remove them during the `Open` lifecycle. An adversary controlling many sybil principals can fill all participant slots, permanently blocking legitimate participants from joining the swap during its active window.

---

### Finding Description

`refresh_buyer_token_e8s` enforces the participant ceiling by counting `self.buyers.len()`: [1](#0-0) 

Once the check passes and the minimum ICP amount is confirmed, the buyer is inserted into `self.buyers` and into `BUYERS_LIST_INDEX` in stable memory. There is no path to remove a buyer from `self.buyers` while the swap is in the `Open` state.

`error_refund_icp` — the only ICP-return path — is gated behind `Aborted` or `Committed` lifecycle: [2](#0-1) 

Even when it executes, it only transfers ICP back to the caller; it never removes the entry from `self.buyers`: [3](#0-2) 

`sweep_icp` similarly marks `transfer_success_timestamp_seconds` but leaves buyer entries in the map.

The constant `MAX_NEURONS_FOR_DIRECT_PARTICIPANTS` is defined in: [4](#0-3) 

and its interaction with basket size is tested explicitly: [5](#0-4) 

The SNS init validation acknowledges a related memory-overflow concern but only addresses it via `MIN_PARTICIPANT_ICP_LOWER_BOUND_E8S`, not slot exhaustion: [6](#0-5) 

---

### Impact Explanation

An adversary who fills `self.buyers` to the `MAX_NEURONS_FOR_DIRECT_PARTICIPANTS / basket_size` ceiling blocks all new principals from calling `refresh_buyer_token_e8s`. Existing participants can still increase their contribution, but no new participant can join. If the swap requires a minimum number of distinct participants (`min_participants`) that cannot be reached because slots are exhausted, the swap will abort. After abort, the adversary recovers all deposited ICP via `sweep_icp` or `error_refund_icp`, making the attack cost-free in the long run. The SNS project loses its decentralization swap entirely.

---

### Likelihood Explanation

The attack requires the adversary to lock `(MAX_NEURONS_FOR_DIRECT_PARTICIPANTS / basket_size) × min_participant_icp_e8s` ICP for the swap duration (days to weeks). Creating sybil principals on the IC is free. For SNS swaps with a large `neuron_basket_construction_parameters.count` (e.g., 3–5, which is typical), the number of required sybil principals drops to tens of thousands. With a low `min_participant_icp_e8s` (the floor is enforced but can be set to a small value by the SNS creator), the total ICP cost is recoverable. A motivated adversary — e.g., a competitor or someone who shorted the SNS token — has clear economic incentive.

---

### Recommendation

When a buyer's ICP balance in their subaccount drops to zero (detectable via a ledger query during `refresh_buyer_token_e8s`) or when `sweep_icp` successfully transfers their ICP out, remove the buyer from `self.buyers` and from `BUYERS_LIST_INDEX`. Alternatively, count only buyers with a non-zero `amount_icp_e8s` toward the participant ceiling, or introduce a withdrawal mechanism during the `Open` phase that removes the buyer entry upon full withdrawal.

---

### Proof of Concept

1. Adversary generates `N = floor(MAX_NEURONS_FOR_DIRECT_PARTICIPANTS / basket_size)` sybil principals.
2. For each sybil principal `P_i`, adversary transfers `min_participant_icp_e8s` ICP to `subaccount(swap_canister, P_i)` on the ICP ledger.
3. Adversary calls `refresh_buyer_token_e8s(P_i, ...)` for each `P_i`. Each call passes the participant-count check (since `(i+1) * basket_size ≤ MAX_NEURONS_FOR_DIRECT_PARTICIPANTS`) and inserts `P_i` into `self.buyers`.
4. After step 3, `self.buyers.len() == N`. Any new principal `P_legit` calling `refresh_buyer_token_e8s` hits the check at line 1187: `(N + 1) * basket_size > MAX_NEURONS_FOR_DIRECT_PARTICIPANTS` → rejected with "The swap has reached the maximum number of direct participants".
5. The swap cannot reach `min_participants` from legitimate users → swap aborts at `swap_due_timestamp_seconds`.
6. Adversary calls `sweep_icp` (or `error_refund_icp`) for each `P_i` → recovers all ICP minus transfer fees. [7](#0-6) [8](#0-7)

### Citations

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

**File:** rs/sns/swap/src/swap.rs (L1925-1936)
```rust
    pub async fn error_refund_icp(
        &self,
        self_canister_id: CanisterId,
        request: &ErrorRefundIcpRequest,
        icp_ledger: &dyn ICRC1Ledger,
    ) -> ErrorRefundIcpResponse {
        // Fail if the request is premature.
        if !(self.lifecycle() == Lifecycle::Aborted || self.lifecycle() == Lifecycle::Committed) {
            return ErrorRefundIcpResponse::new_precondition_error(
                "Error refunds can only be performed when the swap is ABORTED or COMMITTED",
            );
        }
```

**File:** rs/sns/swap/src/swap.rs (L1950-1969)
```rust
        if let Some(buyer_state) = self.buyers.get(&source_principal_id.to_string()) {
            if let Some(transfer) = &buyer_state.icp
                && transfer.transfer_success_timestamp_seconds == 0
            {
                // This buyer has ICP not yet disbursed using the normal mechanism.
                return ErrorRefundIcpResponse::new_precondition_error(format!(
                    "ICP cannot be refunded as principal {} has {} ICP (e8s) in escrow",
                    source_principal_id,
                    buyer_state.amount_icp_e8s()
                ));
            }
            // This buyer has participated in the swap, but all ICP
            // has already been disbursed, either back to the buyer
            // (aborted) or to the SNS Governance canister
            // (committed). Any ICP in this buyer's subaccount must
            // belong to the buyer.
        } else {
            // This buyer is not known to the swap canister. Any
            // balance in a subaccount belongs to the buyer.
        }
```

**File:** rs/nervous_system/common/src/lib.rs (L1-1)
```rust
use by_address::ByAddress;
```

**File:** rs/sns/swap/tests/swap.rs (L4986-5036)
```rust
fn test_refresh_buyer_tokens_no_sns_neuron_baskets_available() {
    let user1 = PrincipalId::new_user_test_id(1);
    let user2 = PrincipalId::new_user_test_id(2);
    let user3 = PrincipalId::new_user_test_id(3);
    let user4 = PrincipalId::new_user_test_id(4);

    let mut swap = SwapBuilder::new()
        .with_sns_governance_canister_id(SNS_GOVERNANCE_CANISTER_ID)
        .with_lifecycle(Open)
        .with_swap_start_due(Some(START_TIMESTAMP_SECONDS), Some(END_TIMESTAMP_SECONDS))
        .with_min_participants(1)
        .with_min_max_participant_icp(2 * E8, 40 * E8)
        .with_min_max_direct_participation(5 * E8, 100 * E8)
        .with_sns_tokens(100_000 * E8)
        // An extremely large basket size, so we can reach MAX_NEURONS_FOR_DIRECT_PARTICIPANTS with
        // a relatively small number of participants.
        .with_neuron_basket_count(33_000)
        .with_neurons_fund_participation()
        .build();

    let params = swap.params.unwrap();

    let amount_user1_0 = 5 * E8;
    let amount_user2_0 = 40 * E8;
    let amount_user3_0 = 40 * E8;
    let amount_user4_0 = 99 * E8 - (amount_user2_0 + amount_user3_0);

    // All tokens but one should be already bought up by users 2 to 4 --> 99 Tokens were bought
    buy_token_ok(&mut swap, &user2, &amount_user2_0, &amount_user2_0);
    buy_token_ok(&mut swap, &user3, &amount_user3_0, &amount_user3_0);
    buy_token_ok(&mut swap, &user4, &amount_user4_0, &amount_user4_0);

    // Make sure the 99 tokens were registered
    assert_eq!(
        swap.get_buyers_total().buyers_total,
        amount_user2_0 + amount_user3_0 + amount_user4_0
    );

    // Make sure that only an amount smaller than the minimum amount to be bought per user is available
    assert!(
        params.max_direct_participation_icp_e8s.unwrap() - swap.get_buyers_total().buyers_total
            < params.min_participant_icp_e8s
    );

    // No user that has not participated in the swap yet can buy this one token left
    buy_token_err(
        &mut swap,
        &user1,
        &amount_user1_0,
        "The swap has reached the maximum number of direct participants",
    );
```

**File:** rs/sns/init/src/lib.rs (L1515-1518)
```rust
    /// (9) min_participant_icp_e8s is at least as big as `MIN_PARTICIPANT_ICP_LOWER_BOUND_E8S`.
    ///     This ensures, that users upon calling `swap.refresh_buyer_token()` must participate
    ///     at least `MIN_PARTICIPANT_ICP_LOWER_BOUND_E8S` Hence, no malicious user can overflow
    ///     node's memory by participating with very low amounts.\
```
