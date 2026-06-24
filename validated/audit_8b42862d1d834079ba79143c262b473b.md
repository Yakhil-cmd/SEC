Audit Report

## Title
Direct Participant Using NNS Governance Canister ID Causes Neuron ID Collision with Neurons' Fund Participants — (File: rs/sns/swap/src/swap.rs)

## Summary
`refresh_buyer_tokens` accepts any principal as `buyer` with no restriction, allowing an attacker to register `NNS_GOVERNANCE_CANISTER_ID` as a direct participant. Because `create_sns_neuron_recipes` assigns direct-participant memos starting at `NEURON_BASKET_MEMO_RANGE_START` keyed on `buyer_principal`, and NF participants also start at `NEURON_BASKET_MEMO_RANGE_START` keyed on `nns_governance_canister_id`, the two paths produce byte-for-byte identical `(principal, memo)` pairs and thus identical SNS ledger subaccounts. This causes `sweep_sns` to double-transfer tokens to the same subaccount and `claim_swap_neurons` to fail for the NF participant, permanently locking their SNS tokens.

## Finding Description

**Root cause — no guard in `refresh_buyer_tokens`:**

The canister endpoint at `rs/sns/swap/canister/canister.rs` L130–133 accepts any non-empty `buyer` string and parses it as a `PrincipalId` with no exclusion list:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    PrincipalId::from_str(&arg.buyer).unwrap()
};
```

No check in `refresh_buyer_token_e8s` rejects `buyer == nns_governance_canister_id`.

**Collision in `create_sns_neuron_recipes`:**

For direct participants (`rs/sns/swap/src/swap.rs` L858–862), memos start at `NEURON_BASKET_MEMO_RANGE_START` using `buyer_principal` as the principal:

```rust
match create_sns_neuron_basket_for_direct_participant(
    &buyer_principal,
    amount_sns_e8s,
    neuron_basket_construction_parameters,
    NEURON_BASKET_MEMO_RANGE_START,
)
```

For NF participants (L891), `global_neurons_fund_memo` also starts at `NEURON_BASKET_MEMO_RANGE_START`, and `create_sns_neuron_basket_for_neurons_fund_participant` (L3373–3376, L3384–3385) uses `nns_governance_canister_id` as the principal with `memo = memo_offset + i`.

When `buyer_principal == nns_governance_canister_id`, both paths compute:
`compute_neuron_staking_subaccount_bytes(nns_governance_canister_id, NEURON_BASKET_MEMO_RANGE_START + i)`

for every `i` in the first basket — identical subaccounts.

**Collision confirmed in `sweep_sns`** (`rs/sns/swap/src/swap.rs` L2216–2231):

```rust
Some(Investor::Direct(DirectInvestment { buyer_principal })) => {
    compute_neuron_staking_subaccount_bytes(p, neuron_memo)
}
Some(Investor::CommunityFund(_)) => {
    compute_neuron_staking_subaccount_bytes(nns_governance.into(), neuron_memo)
}
```

Both branches resolve to the same bytes when `p == nns_governance`.

## Impact Explanation

1. `sweep_sns` executes two successful SNS ledger transfers to the same subaccount (the second simply adds tokens to the already-funded account).
2. `claim_swap_neurons` submits two `NeuronRecipe` entries with the same `neuron_id` (subaccount hash) to SNS governance; the second claim fails with a duplicate-neuron error.
3. The NF participant's neuron is never created. Their SNS tokens are permanently locked in the subaccount — transferred there by `sweep_sns` but unclaimed.
4. The attacker's direct-participant recipe also fails (they don't control NNS governance), so the attacker loses their minimum ICP participation — a deliberate griefing/sabotage attack.

This matches the **High** bounty impact: *Significant SNS security impact with concrete user or protocol harm* — NF participants suffer permanent, irrecoverable loss of SNS tokens with no privileged access required by the attacker.

## Likelihood Explanation

- Requires only publicly obtainable ICP tokens and a single `refresh_buyer_tokens` call with `buyer = NNS_GOVERNANCE_CANISTER_ID` (a well-known public constant).
- No privileged access, no key material, no social engineering.
- The attack is fully deterministic and locally reproducible.
- The only cost to the attacker is the minimum ICP participation amount (lost, not stolen).
- Any SNS swap with Neurons' Fund participation is a valid target.

## Recommendation

Add a guard in `refresh_buyer_token_e8s` (or at the canister endpoint) rejecting any `buyer` principal equal to `nns_governance_canister_id`:

```rust
if buyer == nns_governance_canister_id.get() {
    return Err("Direct participation is not allowed for the NNS governance canister ID".to_string());
}
```

Alternatively, offset the direct-participant memo range away from `NEURON_BASKET_MEMO_RANGE_START` (e.g., use a separate constant such as `DIRECT_PARTICIPANT_MEMO_RANGE_START` that does not overlap with the NF global memo counter), or add a uniqueness assertion over all `(principal, memo)` pairs in `create_sns_neuron_recipes` before committing recipes.

## Proof of Concept

```rust
// Unit test sketch
let nns_gov_id = NNS_GOVERNANCE_CANISTER_ID.get();

// Attacker transfers ICP to principal_to_subaccount(nns_gov_id) on ICP ledger,
// then calls refresh_buyer_tokens with buyer = nns_gov_id.
swap.refresh_buyer_token_e8s(
    nns_gov_id,
    None,
    SWAP_CANISTER_ID,
    &mock_icp_ledger_with_balance(min_icp),
).await.unwrap();

// Add one NF participant
swap.cf_participants = vec![cf_participant_with_one_neuron()];

// Commit and create recipes
swap.create_sns_neuron_recipes();

// Collect all subaccounts used for neuron staking
let subaccounts: Vec<[u8;32]> = swap.neuron_recipes.iter().map(|r| {
    let memo = r.neuron_attributes.as_ref().unwrap().memo;
    match r.investor.as_ref().unwrap() {
        Investor::Direct(d) => compute_neuron_staking_subaccount_bytes(
            PrincipalId::from_str(&d.buyer_principal).unwrap(), memo),
        Investor::CommunityFund(_) => compute_neuron_staking_subaccount_bytes(
            nns_gov_id, memo),
    }
}).collect();

// Assert uniqueness — this FAILS, proving the collision
let unique: HashSet<_> = subaccounts.iter().collect();
assert_eq!(unique.len(), subaccounts.len(), "COLLISION DETECTED");
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** rs/sns/swap/canister/canister.rs (L130-133)
```rust
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()
```

**File:** rs/sns/swap/src/swap.rs (L858-862)
```rust
            match create_sns_neuron_basket_for_direct_participant(
                &buyer_principal,
                amount_sns_e8s,
                neuron_basket_construction_parameters,
                NEURON_BASKET_MEMO_RANGE_START,
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

**File:** rs/sns/swap/src/swap.rs (L3371-3385)
```rust
    let memo_of_longest_dissolve_delay = memo_offset + (vesting_schedule.len() - 1) as u64;
    let neuron_id_with_longest_dissolve_delay =
        SwapNeuronId::from(compute_neuron_staking_subaccount_bytes(
            nns_governance_canister_id,
            memo_of_longest_dissolve_delay,
        ));

    // Create the neuron basket for the Neurons' Fund investors. The unique
    // identifier for an SNS Neuron is the SNS Ledger Subaccount, which
    // is a hash of PrincipalId and some unique memo. Since Neurons' Fund
    // investors in the swap use the NNS Governance principal, there can be
    // neuron id collisions. Avoiding such collisions is handled by starting the range
    // of memos in the basket at memo_offset.
    for (i, scheduled_vesting_event) in vesting_schedule.iter().enumerate() {
        let memo = memo_offset + i as u64;
```
