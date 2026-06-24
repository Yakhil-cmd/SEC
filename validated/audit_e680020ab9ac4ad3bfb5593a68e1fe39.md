Audit Report

## Title
Missing Anonymous Principal Validation in SNS Swap `refresh_buyer_tokens` - (File: `rs/sns/swap/canister/canister.rs`)

## Summary
The `refresh_buyer_tokens` update endpoint in the SNS Swap canister accepts the anonymous principal (`2vxsx-fae`) as a valid `buyer` argument without rejection. An unprivileged attacker can fund the anonymous principal's subaccount with ICP and register it as a swap participant, causing SNS tokens to be minted into permanently inaccessible neurons on finalization. The same mechanism can exhaust the `MAX_NEURONS_FOR_DIRECT_PARTICIPANTS` cap, denying legitimate buyers entry to the swap.

## Finding Description
In `rs/sns/swap/canister/canister.rs` at L130–134, when `arg.buyer` is non-empty the handler does:

```rust
PrincipalId::from_str(&arg.buyer).unwrap()
```

with no subsequent identity check before passing the result to `refresh_buyer_token_e8s`. The anonymous principal `"2vxsx-fae"` is syntactically valid and parses successfully.

`refresh_buyer_token_e8s` (`rs/sns/swap/src/swap.rs`, L1134–1312) performs only lifecycle, confirmation-text, and ICP-amount checks. It computes `principal_to_subaccount(&buyer)` (L3273–3279), queries the ICP ledger balance for that subaccount, and — if the balance meets the minimum — inserts the principal into `self.buyers` (L1285–1288) with no identity guard.

`is_valid_principal` (`rs/sns/swap/src/swap.rs`, L3269–3271) only checks non-emptiness and parseability; the anonymous principal satisfies both conditions. `DirectInvestment::validate()` (`rs/sns/swap/src/types.rs`, L671–675) calls `is_valid_principal` but is invoked only on stored neuron recipes after finalization, not during the live registration path.

On swap finalization, `create_sns_neuron_basket_for_direct_participant` (`rs/sns/swap/src/swap.rs`, L3299–3352) iterates over all entries in `self.buyers`, including the anonymous principal, and mints `SnsNeuronRecipe` entries with `buyer_principal = "2vxsx-fae"`. Because no identity controls the anonymous principal, those SNS neurons are permanently locked and irrecoverable.

No guard equivalent to the ckETH minter's `parse_principal_from_slice` anonymous-principal rejection exists anywhere in the SNS Swap buyer registration path.

## Impact Explanation
Two concrete harms result:

1. **Permanent loss of SNS tokens**: SNS tokens allocated to the anonymous principal's neuron basket are minted but permanently inaccessible. This constitutes irreversible token loss within the SNS framework, matching the High impact category: "Significant SNS security impact with concrete user or protocol harm."

2. **Swap participation DoS**: Each anonymous-principal registration consumes one slot against `MAX_NEURONS_FOR_DIRECT_PARTICIPANTS`. An attacker spending enough ICP can fill all remaining slots, preventing legitimate buyers from participating. This matches the High impact category: "Application/platform-level DoS… not based on raw volumetric DDoS."

Severity: **High ($2,000–$10,000)**.

## Likelihood Explanation
The endpoint is a public `#[update]` call requiring no special privileges. The attacker must spend real ICP (at minimum `min_participant_icp_e8s` per registration), which limits large-scale exploitation but does not prevent targeted attacks against specific swaps. The attack is fully permissionless, deterministic, and repeatable for any open SNS swap. Likelihood: **Low-to-Medium**.

## Recommendation
Add an explicit anonymous principal rejection at the top of `refresh_buyer_tokens` in `rs/sns/swap/canister/canister.rs`, immediately after parsing the principal:

```rust
if p == PrincipalId::new_anonymous() {
    panic!("anonymous principal is not allowed as a swap buyer");
}
```

Alternatively, extend `is_valid_principal` in `rs/sns/swap/src/swap.rs` to reject the anonymous principal and invoke it from the `refresh_buyer_tokens` handler before calling `refresh_buyer_token_e8s`. The same guard should cover the management canister principal for defense-in-depth.

## Proof of Concept
1. Deploy or identify an open SNS swap canister.
2. Compute the anonymous principal's subaccount: `principal_to_subaccount(&PrincipalId::new_anonymous())` — a deterministic 32-byte value.
3. Transfer at least `min_participant_icp_e8s` ICP to `Account { owner: swap_canister_id, subaccount: Some(<computed above>) }` on the ICP ledger.
4. Send an ingress update to the swap canister:
   ```
   refresh_buyer_tokens({ buyer = "2vxsx-fae", confirmation_text = null })
   ```
5. Observe that the call succeeds and `swap.buyers` now contains an entry for `"2vxsx-fae"`.
6. Trigger or await swap finalization; observe that `create_sns_neuron_basket_for_direct_participant` produces `SnsNeuronRecipe` entries with `buyer_principal = "2vxsx-fae"` — permanently locking the allocated SNS tokens.

A unit test can be written against `refresh_buyer_token_e8s` directly, passing `PrincipalId::new_anonymous()` as `buyer` with a mock ICP ledger returning a sufficient balance, and asserting the call returns an error rather than inserting the anonymous principal into `self.buyers`.