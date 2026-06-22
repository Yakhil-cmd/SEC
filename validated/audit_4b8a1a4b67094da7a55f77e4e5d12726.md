### Title
Dust SNS Token Accumulation in Swap Canister Due to Integer Truncation in `Swap::scale` — (File: rs/sns/swap/src/swap.rs)

---

### Summary

The SNS swap canister uses integer-truncating division in `Swap::scale` to allocate SNS tokens proportionally to each participant. Because every participant's allocation is floored to the nearest e8, the sum of all allocated tokens is strictly less than the total offered token pool. The unallocated remainder (dust) is permanently locked in the swap canister after finalization, with no admin or governance-accessible sweep function to recover it.

---

### Finding Description

In `create_sns_neuron_recipes`, each direct participant's SNS token allocation is computed as:

```rust
let amount_sns_e8s = Swap::scale(
    buyer_state.amount_icp_e8s(),
    sns_being_offered_e8s,
    total_participant_icp_e8s,
);
``` [1](#0-0) 

`Swap::scale` performs the proportional calculation `(buyer_icp × sns_total) / total_icp`, which is integer division and therefore truncates. The same truncating scale is applied to every Neurons' Fund participant: [2](#0-1) 

A running total is kept:

```rust
let mut total_sns_tokens_sold_e8s: u64 = 0;
``` [3](#0-2) 

Because each per-participant result is floored, `total_sns_tokens_sold_e8s < sns_being_offered_e8s` whenever there are multiple participants whose ICP contributions do not divide evenly into the token pool. The difference — up to `(N − 1)` e8s for N participants — remains in the swap canister's SNS ledger account after `sweep_sns` completes.

`sweep_sns` only iterates over `self.neuron_recipes` and transfers each recipe's exact `amount_e8s` to the corresponding neuron staking subaccount: [4](#0-3) 

There is no subsequent step in `finalize_swap` that transfers the residual balance to the SNS treasury, governance canister, or any other recoverable destination. The `FinalizeSwapResponse` tracks `sweep_icp_result`, `sweep_sns_result`, `claim_neuron_result`, etc., but contains no field for a residual-token sweep: [5](#0-4) 

An analogous truncation issue exists in NNS governance reward distribution, where `(used_voting_rights × total_available_e8s_equivalent_float / total_voting_rights) as u64` truncates per-neuron maturity rewards: [6](#0-5) 

The `e8s_equivalent_to_be_rolled_over` mechanism only rolls over the full purse when `settled_proposals.is_empty()`; when proposals are settled, the truncation remainder is silently discarded and never rolled forward: [7](#0-6) 

The SNS governance `distribute_rewards` has the same rollover-only-on-empty-proposals behavior: [8](#0-7) 

---

### Impact Explanation

After every committed SNS swap, a small number of SNS tokens (at most `N − 1` e8s for N participants) remain permanently locked in the swap canister. The swap canister exposes no public method to transfer this residual balance. Because the swap canister is in a terminal lifecycle state after finalization, the only recovery path would be a canister upgrade by the SNS root — an out-of-band governance action not anticipated by the protocol. For high-value SNS tokens or swaps with many participants, the aggregate dust across all SNS launches is a non-trivial permanent loss of token supply from circulation.

---

### Likelihood Explanation

This condition is triggered by every SNS swap that commits with more than one participant whose ICP contributions are not perfectly divisible into the offered token pool — which is virtually every real-world SNS swap. The truncation is deterministic and unavoidable given the current integer arithmetic in `Swap::scale`. No special attacker action is required; normal participation by any set of buyers is sufficient to produce the dust.

---

### Recommendation

1. After `sweep_sns` completes, compute the residual balance (`sns_being_offered_e8s − total_sns_tokens_sold_e8s`) and transfer it to the SNS governance treasury account in the same finalization flow.
2. Alternatively, allocate the residual to the last (or largest) participant to avoid an extra ledger call.
3. For NNS/SNS governance reward rounds, roll the per-round truncation remainder forward into the next round's purse even when proposals were settled, analogous to the existing rollover logic for empty rounds.

---

### Proof of Concept

Consider a swap with `sns_being_offered_e8s = 100` and two participants:
- Participant A: 1 ICP out of 3 ICP total → `scale(1, 100, 3) = 33` SNS tokens
- Participant B: 2 ICP out of 3 ICP total → `scale(2, 100, 3) = 66` SNS tokens
- `total_sns_tokens_sold_e8s = 99`
- Residual = `100 − 99 = 1` SNS token locked in the swap canister forever [9](#0-8)

### Citations

**File:** rs/sns/swap/src/swap.rs (L815-852)
```rust
        let sns_being_offered_e8s = params.sns_token_e8s;
        // Note that this value has to be > 0 as we have > 0
        // participants each with > 0 ICP contributed.
        let total_participant_icp_e8s = match NonZeroU64::try_from(
            self.current_total_participation_e8s(),
        ) {
            Ok(total_participant_icp_e8s) => total_participant_icp_e8s,
            Err(error_message) => {
                log!(
                    ERROR,
                    "Halting create_sns_neuron_recipes(). Swap is finalizing with 0 total participation: {:?}",
                    error_message
                );
                return SweepResult::new_with_global_failures(1);
            }
        };

        // Keep track of SNS tokens sold just to check that the amount
        // is correct at the end.
        let mut total_sns_tokens_sold_e8s: u64 = 0;

        // =====================================================================
        // ===            This is where the actual swap happens              ===
        // =====================================================================
        for (buyer_principal, buyer_state) in self.buyers.iter_mut() {
            // The case that on a previous attempt at creating this neuron recipe, it was
            // successfully created and recorded. Count the number of neuron recipes that
            // would have been created.
            if buyer_state.has_created_neuron_recipes == Some(true) {
                sweep_result.skipped += neuron_basket_construction_parameters.count as u32;
                continue;
            }

            let amount_sns_e8s = Swap::scale(
                buyer_state.amount_icp_e8s(),
                sns_being_offered_e8s,
                total_participant_icp_e8s,
            );
```

**File:** rs/sns/swap/src/swap.rs (L920-924)
```rust
                    let amount_sns_e8s = Swap::scale(
                        neurons_fund_neuron.amount_icp_e8s,
                        sns_being_offered_e8s,
                        total_participant_icp_e8s,
                    );
```

**File:** rs/sns/swap/src/swap.rs (L2165-2200)
```rust
    pub async fn sweep_sns(
        &mut self,
        now_fn: fn(bool) -> u64,
        sns_ledger: &dyn ICRC1Ledger,
    ) -> SweepResult {
        if self.lifecycle() != Lifecycle::Committed {
            log!(
                ERROR,
                "Halting sweep_sns(). SNS Tokens cannot be distributed if \
                Lifecycle is not COMMITTED. Current Lifecycle: {:?}",
                self.lifecycle()
            );
            return SweepResult::new_with_global_failures(1);
        }

        let init = match self.init_and_validate() {
            Ok(init) => init,
            Err(error_message) => {
                log!(
                    ERROR,
                    "Halting sweep_sns(). State is missing or corrupted: {:?}",
                    error_message
                );
                return SweepResult::new_with_global_failures(1);
            }
        };

        // The following methods are safe to call since we validated Init in the above block
        let sns_governance = init.sns_governance_or_panic();
        let nns_governance = init.nns_governance_or_panic();
        let sns_transaction_fee_tokens = Tokens::from_e8s(init.transaction_fee_e8s_or_panic());

        let mut sweep_result = SweepResult::default();

        for recipe in self.neuron_recipes.iter_mut() {
            let neuron_memo = match recipe.neuron_attributes.as_ref() {
```

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L862-881)
```text
message FinalizeSwapResponse {
  SweepResult sweep_icp_result = 1;

  SweepResult sweep_sns_result = 2;

  SweepResult claim_neuron_result = 3;

  SetModeCallResult set_mode_call_result = 4;

  SetDappControllersCallResult set_dapp_controllers_call_result = 5;

  SettleCommunityFundParticipationResult settle_community_fund_participation_result = 6;

  SweepResult create_sns_neuron_recipes_result = 8;

  SettleNeuronsFundParticipationResult settle_neurons_fund_participation_result = 9;

  // Explains what (if anything) went wrong.
  optional string error_message = 7;
}
```

**File:** rs/nns/governance/src/governance.rs (L6724-6725)
```rust
                    let reward = (used_voting_rights * total_available_e8s_equivalent_float
                        / total_voting_rights) as u64;
```

**File:** rs/nns/governance/src/reward/calculation.rs (L120-126)
```rust
    pub(crate) fn e8s_equivalent_to_be_rolled_over(&self) -> u64 {
        if self.rewards_rolled_over() {
            self.total_available_e8s_equivalent
        } else {
            0
        }
    }
```

**File:** rs/sns/governance/src/types.rs (L2054-2059)
```rust
    pub(crate) fn e8s_equivalent_to_be_rolled_over(&self) -> u64 {
        if self.rewards_rolled_over() {
            self.total_available_e8s_equivalent.unwrap_or_default()
        } else {
            0
        }
```
