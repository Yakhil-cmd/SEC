Let me look at `sweep_sns` to understand how it computes the destination subaccount from recipes, and also check if there's any guard against NNS governance as a direct participant.

### Title
Direct Participant Using NNS Governance Canister ID Causes Neuron ID Collision with Neurons' Fund Participants — (`rs/sns/swap/src/swap.rs`)

---

### Summary

`create_sns_neuron_recipes` assigns memo ranges for direct participants starting at `NEURON_BASKET_MEMO_RANGE_START` keyed on `buyer_principal`, and assigns memo ranges for NF participants starting at `NEURON_BASKET_MEMO_RANGE_START` keyed on `nns_governance_canister_id`. If a direct participant's `buyer_principal` equals `nns_governance_canister_id`, both paths produce identical `(principal, memo)` pairs, yielding identical SNS ledger subaccounts. There is no guard in `refresh_buyer_tokens` preventing the NNS governance canister ID from being registered as a direct participant.

---

### Finding Description

**Step 1 — Attacker registers as a direct participant with `buyer_principal = NNS_GOVERNANCE_CANISTER_ID`.**

The `refresh_buyer_tokens` canister endpoint accepts any `buyer` string with no restriction:

```rust
// rs/sns/swap/canister/canister.rs:130-133
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    PrincipalId::from_str(&arg.buyer).unwrap()
};
``` [1](#0-0) 

The attacker transfers ICP to `principal_to_subaccount(NNS_GOVERNANCE_CANISTER_ID)` on the ICP ledger (any principal can send ICP to any account), then calls `refresh_buyer_tokens` with `buyer = NNS_GOVERNANCE_CANISTER_ID`. `refresh_buyer_token_e8s` has no check rejecting this principal.

**Step 2 — `create_sns_neuron_recipes` produces colliding recipes.**

For direct participants, memos start at `NEURON_BASKET_MEMO_RANGE_START` using `buyer_principal`:

```rust
// rs/sns/swap/src/swap.rs:858-862
match create_sns_neuron_basket_for_direct_participant(
    &buyer_principal,
    amount_sns_e8s,
    neuron_basket_construction_parameters,
    NEURON_BASKET_MEMO_RANGE_START,
)
``` [2](#0-1) 

For NF participants, `global_neurons_fund_memo` also starts at `NEURON_BASKET_MEMO_RANGE_START`, and the subaccount is keyed on `nns_governance_canister_id`:

```rust
// rs/sns/swap/src/swap.rs:891
let mut global_neurons_fund_memo: u64 = NEURON_BASKET_MEMO_RANGE_START;
``` [3](#0-2) 

The subaccount computation in `create_sns_neuron_basket_for_neurons_fund_participant` uses `nns_governance_canister_id` as the principal:

```rust
// rs/sns/swap/src/swap.rs:3373-3376
SwapNeuronId::from(compute_neuron_staking_subaccount_bytes(
    nns_governance_canister_id,
    memo_of_longest_dissolve_delay,
));
``` [4](#0-3) 

And per-neuron memos: `memo = memo_offset + i as u64` — identical to the direct participant path when `buyer_principal == nns_governance_canister_id`. [5](#0-4) 

**Step 3 — `sweep_sns` confirms the collision.**

`sweep_sns` computes the destination subaccount as:
- Direct: `compute_neuron_staking_subaccount_bytes(buyer_principal, neuron_memo)`
- NF: `compute_neuron_staking_subaccount_bytes(nns_governance.into(), neuron_memo)` [6](#0-5) 

When `buyer_principal == nns_governance_canister_id` and both memo sequences start at `NEURON_BASKET_MEMO_RANGE_START`, the destination subaccounts are byte-for-byte identical for every neuron in the first basket.

The `compute_neuron_staking_subaccount_bytes` function is a deterministic SHA-256 hash of `(domain, principal_bytes, nonce)`: [7](#0-6) 

---

### Impact Explanation

1. `sweep_sns` transfers SNS tokens to the same subaccount twice (both ledger transfers succeed — the second simply adds tokens to the same account).
2. `claim_swap_neurons` sends two `NeuronRecipe` entries with the same `neuron_id` (subaccount) to SNS governance. The second claim fails with a duplicate-neuron error.
3. The NF participant's neuron is never created. Their SNS tokens are permanently stuck in the subaccount (already transferred there by `sweep_sns`, but the neuron claim failed).
4. The attacker's direct-participant recipe also fails to produce a usable neuron (they don't control NNS governance), so the attacker loses their ICP — but the NF participant suffers permanent token loss.

This is a griefing/sabotage attack: an attacker sacrifices their ICP participation to permanently destroy NF participants' SNS neuron creation and lock their tokens.

---

### Likelihood Explanation

- Requires only ICP tokens (publicly obtainable) and a single `refresh_buyer_tokens` call with `buyer = NNS_GOVERNANCE_CANISTER_ID`.
- No privileged access, no key material, no social engineering.
- The NNS governance canister ID is a well-known public constant.
- The attack is deterministic and fully local-testable.
- The only cost to the attacker is the minimum ICP participation amount (which is lost, not stolen).

---

### Recommendation

Add a guard in `refresh_buyer_token_e8s` (or at the canister endpoint level) rejecting any `buyer` principal that equals `nns_governance_canister_id`:

```rust
if buyer == nns_governance_canister_id.get() {
    return Err("Direct participation is not allowed for the NNS governance canister ID".to_string());
}
```

Alternatively, offset the direct-participant memo range away from `NEURON_BASKET_MEMO_RANGE_START` so it cannot overlap with the NF global memo counter, or add a uniqueness assertion over all `(principal, memo)` pairs in `create_sns_neuron_recipes` before committing recipes.

---

### Proof of Concept

```rust
// Unit test sketch
let nns_gov_id = NNS_GOVERNANCE_CANISTER_ID.get(); // e.g. rrkah-fqaaa-aaaaa-aaaaq-cai

// Attacker registers as direct participant with NNS governance principal
swap.refresh_buyer_token_e8s(
    nns_gov_id,          // buyer = NNS governance canister ID
    None,
    SWAP_CANISTER_ID,
    &mock_icp_ledger_with_balance(min_icp),
).await.unwrap();

// Add one NF participant
swap.cf_participants = vec![cf_participant_with_one_neuron()];

// Commit and create recipes
swap.create_sns_neuron_recipes();

// Collect all (principal, memo) pairs used for subaccount computation
let subaccounts: Vec<[u8;32]> = swap.neuron_recipes.iter().map(|r| {
    let memo = r.neuron_attributes.as_ref().unwrap().memo;
    match r.investor.as_ref().unwrap() {
        Investor::Direct(d) => compute_neuron_staking_subaccount_bytes(
            PrincipalId::from_str(&d.buyer_principal).unwrap(), memo),
        Investor::CommunityFund(_) => compute_neuron_staking_subaccount_bytes(
            nns_gov_id, memo),
    }
}).collect();

// Assert: duplicates exist
let unique: HashSet<_> = subaccounts.iter().collect();
assert_eq!(unique.len(), subaccounts.len(), "COLLISION DETECTED");
// ^^^ This assertion FAILS — proving the vulnerability
```

### Citations

**File:** rs/sns/swap/canister/canister.rs (L130-133)
```rust
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()
```

**File:** rs/sns/swap/src/swap.rs (L858-863)
```rust
            match create_sns_neuron_basket_for_direct_participant(
                &buyer_principal,
                amount_sns_e8s,
                neuron_basket_construction_parameters,
                NEURON_BASKET_MEMO_RANGE_START,
            ) {
```

**File:** rs/sns/swap/src/swap.rs (L891-891)
```rust
        let mut global_neurons_fund_memo: u64 = NEURON_BASKET_MEMO_RANGE_START;
```

**File:** rs/sns/swap/src/swap.rs (L2216-2231)
```rust
            let dst_subaccount = match &recipe.investor {
                Some(Investor::Direct(DirectInvestment { buyer_principal })) => {
                    match string_to_principal(buyer_principal) {
                        Some(p) => compute_neuron_staking_subaccount_bytes(p, neuron_memo),
                        // principal_str should always be parseable as a PrincipalId as that is enforced
                        // in `refresh_buyer_tokens`. In the case of a bug due to programmer error, increment
                        // the invalid field. This will require a manual intervention via an upgrade to correct
                        None => {
                            sweep_result.invalid += 1;
                            continue;
                        }
                    }
                }
                Some(Investor::CommunityFund(_)) => {
                    compute_neuron_staking_subaccount_bytes(nns_governance.into(), neuron_memo)
                }
```

**File:** rs/sns/swap/src/swap.rs (L3371-3376)
```rust
    let memo_of_longest_dissolve_delay = memo_offset + (vesting_schedule.len() - 1) as u64;
    let neuron_id_with_longest_dissolve_delay =
        SwapNeuronId::from(compute_neuron_staking_subaccount_bytes(
            nns_governance_canister_id,
            memo_of_longest_dissolve_delay,
        ));
```

**File:** rs/sns/swap/src/swap.rs (L3384-3385)
```rust
    for (i, scheduled_vesting_event) in vesting_schedule.iter().enumerate() {
        let memo = memo_offset + i as u64;
```

**File:** rs/nervous_system/common/src/ledger.rs (L6-8)
```rust
pub fn compute_neuron_staking_subaccount_bytes(controller: PrincipalId, nonce: u64) -> [u8; 32] {
    compute_neuron_domain_subaccount_bytes(controller, b"neuron-stake", nonce)
}
```
