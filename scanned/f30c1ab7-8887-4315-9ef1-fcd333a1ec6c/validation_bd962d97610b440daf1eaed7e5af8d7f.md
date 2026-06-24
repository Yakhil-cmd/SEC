### Title
SNS Swap `validate_participation_constraints` Underestimates Minimum SNS Tokens Per Participant When Neurons' Fund Participates, Causing Neuron Basket Creation Failure and Loss of Funds - (File: rs/sns/init/src/lib.rs)

### Summary

The `validate_participation_constraints` function in the SNS initialization library validates that each direct participant will receive enough SNS tokens to form a full neuron basket. However, the validation uses `max_direct_participation_icp_e8s` as the denominator when computing the minimum SNS tokens per participant, while the actual token distribution at finalization uses `total_participant_icp_e8s` — which includes Neurons' Fund (NF) participation — as the denominator. When NF participates, the actual SNS tokens received by a direct participant can fall below `count * (neuron_minimum_stake_e8s + transaction_fee_e8s)`, causing neuron basket creation to fail silently and the participant's ICP to be permanently lost.

### Finding Description

**Validation check (rs/sns/init/src/lib.rs, check #8):**

```rust
let min_participant_sns_e8s = min_participant_icp_e8s as u128
    * initial_swap_amount_e8s as u128
    / max_direct_participation_icp_e8s as u128;   // ← uses only direct cap

let min_participant_icp_e8s_big_enough = min_participant_sns_e8s
    >= neuron_basket_construction_parameters_count as u128
        * (neuron_minimum_stake_e8s + sns_transaction_fee_e8s) as u128;
``` [1](#0-0) 

This check passes when `max_direct_participation_icp_e8s` is the total ICP denominator. But at finalization, `create_sns_neuron_recipes` computes each participant's SNS allocation using `total_participant_icp_e8s`, which includes NF contributions:

```rust
let total_participant_icp_e8s = match NonZeroU64::try_from(
    self.current_total_participation_e8s(),   // direct + NF
) { ... };

let amount_sns_e8s = Swap::scale(
    buyer_state.amount_icp_e8s(),
    sns_being_offered_e8s,
    total_participant_icp_e8s,   // ← larger denominator when NF participates
);
``` [2](#0-1) 

The `Swap::scale` function performs integer division:

```rust
fn scale(amount_icp_e8s: u64, total_sns_e8s: u64, total_icp_e8s: NonZeroU64) -> u64 {
    let r = (amount_icp_e8s as u128)
        .saturating_mul(total_sns_e8s as u128)
        .div(NonZeroU128::from(total_icp_e8s));
    r as u64
}
``` [3](#0-2) 

When NF participates, `total_participant_icp_e8s > max_direct_participation_icp_e8s`, so `amount_sns_e8s` is strictly less than what the validation check assumed. The result is passed to `create_sns_neuron_basket_for_direct_participant`, which calls `generate_vesting_schedule` → `apportion_approximately_equally` to split the total into `count` neuron slots:

```rust
fn generate_vesting_schedule(
    &self,
    total_amount_e8s: u64,
) -> Result<Vec<ScheduledVestingEvent>, String> {
    ...
    let chunks_e8s = apportion_approximately_equally(total_amount_e8s, self.count)?;
    ...
}
``` [4](#0-3) 

Neither `generate_vesting_schedule` nor `apportion_approximately_equally` checks that each chunk meets `neuron_minimum_stake_e8s`. Chunks below the minimum are silently produced. When `sweep_sns` subsequently transfers these sub-minimum amounts to neuron subaccounts, and `claim_swap_neurons` in SNS Governance attempts to claim them, the claim fails because the precondition `stake_e8s >= neuron_minimum_stake_e8s` is violated:

```
/// Preconditions:
/// - Each NeuronRecipe's `stake_e8s` is at least neuron_minimum_stake_e8s
///   as defined in the `NervousSystemParameters`
pub fn claim_swap_neurons(...)
``` [5](#0-4) 

The participant's ICP has already been swept to SNS Governance via `sweep_icp`. Their SNS tokens are either stuck in the neuron subaccount or the recipe is marked `ClaimedStatus::Invalid` and never retried, resulting in a complete loss of funds.

### Impact Explanation

A direct swap participant who contributes exactly `min_participant_icp_e8s` ICP — the minimum allowed — can have their ICP permanently transferred to SNS Governance while their SNS neuron basket fails to be created. The ICP cannot be refunded (the swap is committed), and the SNS tokens are stranded. This is a complete, irreversible loss of funds for the participant. The magnitude scales with `min_participant_icp_e8s`, which can be substantial (e.g., hundreds of ICP in real SNS launches).

### Likelihood Explanation

This requires:
1. A swap configured with Neurons' Fund participation enabled (`neurons_fund_participation: true`).
2. The NF actually participates at finalization.
3. A direct participant contributing close to `min_participant_icp_e8s`.

All three conditions are part of the intended, normal operation of the SNS swap. NF participation is a core feature and is enabled in many real SNS launches. No privileged access, key compromise, or majority attack is required. Any unprivileged user who participates in such a swap at the minimum contribution level is at risk.

### Recommendation

Replace `max_direct_participation_icp_e8s` with the maximum possible total participation (direct + NF) in the denominator of check #8:

```rust
// Use max total participation (direct + NF) as the worst-case denominator
let max_total_participation_icp_e8s = max_direct_participation_icp_e8s
    .saturating_add(max_neurons_fund_participation_icp_e8s.unwrap_or(0));

let min_participant_sns_e8s = min_participant_icp_e8s as u128
    * initial_swap_amount_e8s as u128
    / max_total_participation_icp_e8s as u128;
```

Additionally, add a runtime guard in `create_sns_neuron_basket_for_direct_participant` and `create_sns_neuron_basket_for_neurons_fund_participant` that returns an error if `amount_sns_token_e8s / count < neuron_minimum_stake_e8s + transaction_fee_e8s`, so that finalization fails loudly rather than silently producing unclaimable recipes.

### Proof of Concept

**Setup:**
- `min_participant_icp_e8s = 1_000_000_000` (10 ICP)
- `max_direct_participation_icp_e8s = 10_000_000_000` (100 ICP)
- `initial_swap_amount_e8s = 1_000_000_000` (10 SNS tokens)
- `neuron_basket_construction_parameters.count = 3`
- `neuron_minimum_stake_e8s = 30_000_000` (0.3 SNS tokens)
- `transaction_fee_e8s = 10_000`
- `max_neurons_fund_participation_icp_e8s = 90_000_000_000` (900 ICP)

**Validation check (passes):**
```
min_participant_sns_e8s = 1e9 * 1e9 / 1e10 = 100_000_000 (1 SNS token)
required = 3 * (30_000_000 + 10_000) = 90_030_000
100_000_000 >= 90_030_000  ✓  (validation passes)
```

**At finalization (NF contributes 90 ICP, direct total = 100 ICP):**
```
total_participant_icp_e8s = 100 ICP + 90 ICP = 190 ICP = 19_000_000_000
amount_sns_e8s for min participant = 1e9 * 1e9 / 19e9 = 52_631_578
per_neuron = 52_631_578 / 3 = 17_543_859  <  neuron_minimum_stake_e8s (30_000_000)
```

The participant's ICP (10 ICP) is swept to SNS Governance. Their three neuron recipes each have `17_543_859` e8s, below `neuron_minimum_stake_e8s`. `claim_swap_neurons` rejects them. The participant loses 10 ICP with no recourse. [1](#0-0) [6](#0-5) [7](#0-6)

### Citations

**File:** rs/sns/init/src/lib.rs (L1635-1655)
```rust
        // (8)
        let min_participant_sns_e8s = min_participant_icp_e8s as u128
            * initial_swap_amount_e8s as u128
            / max_direct_participation_icp_e8s as u128;

        let min_participant_icp_e8s_big_enough = min_participant_sns_e8s
            >= neuron_basket_construction_parameters_count as u128
                * (neuron_minimum_stake_e8s + sns_transaction_fee_e8s) as u128;

        if !min_participant_icp_e8s_big_enough {
            return Err(format!(
                "Error: min_participant_icp_e8s ({min_participant_icp_e8s}) is too small. It needs to be \
                 large enough to ensure that participants will end up with \
                 enough SNS tokens to form {neuron_basket_construction_parameters_count} SNS neurons, each of which \
                 require at least {neuron_minimum_stake_e8s} SNS e8s, plus {sns_transaction_fee_e8s} e8s in transaction \
                 fees. More precisely, the following inequality must hold: \
                 min_participant_icp_e8s >= neuron_basket_count \
                 * (neuron_minimum_stake_e8s + transaction_fee_e8s) \
                 * max_direct_participation_icp_e8s / initial_swap_amount_e8s",
            ));
        }
```

**File:** rs/sns/swap/src/swap.rs (L157-188)
```rust
impl NeuronBasketConstructionParameters {
    /// Chops `total_amount_e8s` into `self.count` pieces. Each gets doled out
    /// every `self.dissolve_delay_seconds`, starting from 0.
    ///
    /// # Arguments
    /// * `total_amount_e8s` - The total amount of tokens (in e8s) to be chopped up.
    fn generate_vesting_schedule(
        &self,
        total_amount_e8s: u64,
    ) -> Result<Vec<ScheduledVestingEvent>, String> {
        if self.count == 0 {
            return Err(
                "NeuronBasketConstructionParameters.count must be greater than zero".to_string(),
            );
        }

        let dissolve_delay_seconds_list = (0..(self.count))
            .map(|i| i * self.dissolve_delay_interval_seconds)
            .collect::<Vec<u64>>();

        let chunks_e8s = apportion_approximately_equally(total_amount_e8s, self.count)?;
        Ok(dissolve_delay_seconds_list
            .into_iter()
            .zip(chunks_e8s)
            .map(
                |(dissolve_delay_seconds, amount_e8s)| ScheduledVestingEvent {
                    dissolve_delay_seconds,
                    amount_e8s,
                },
            )
            .collect())
    }
```

**File:** rs/sns/swap/src/swap.rs (L742-751)
```rust
    fn scale(amount_icp_e8s: u64, total_sns_e8s: u64, total_icp_e8s: NonZeroU64) -> u64 {
        assert!(amount_icp_e8s <= u64::from(total_icp_e8s));
        // Note that the multiplication cannot overflow as both factors fit in 64 bits.
        let r = (amount_icp_e8s as u128)
            .saturating_mul(total_sns_e8s as u128)
            .div(NonZeroU128::from(total_icp_e8s));
        // This follows logically from the initial assert `amount_icp_e8s <= total_icp_e8s`.
        assert!(r <= u64::MAX as u128);
        r as u64
    }
```

**File:** rs/sns/swap/src/swap.rs (L818-882)
```rust
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

            let Some(buyer_principal) = string_to_principal(buyer_principal) else {
                sweep_result.invalid += neuron_basket_construction_parameters.count as u32;
                continue;
            };
            match create_sns_neuron_basket_for_direct_participant(
                &buyer_principal,
                amount_sns_e8s,
                neuron_basket_construction_parameters,
                NEURON_BASKET_MEMO_RANGE_START,
            ) {
                Ok(direct_participant_sns_neuron_recipes) => {
                    self.neuron_recipes
                        .extend(direct_participant_sns_neuron_recipes);
                    total_sns_tokens_sold_e8s =
                        total_sns_tokens_sold_e8s.saturating_add(amount_sns_e8s);
                    sweep_result.success += neuron_basket_construction_parameters.count as u32;
                    buyer_state.has_created_neuron_recipes = Some(true);
                }
                Err(error_message) => {
                    log!(
                        ERROR,
                        "Error creating a neuron basked for identity {}. Reason: {}",
                        buyer_principal,
                        error_message
                    );
                    sweep_result.failure += neuron_basket_construction_parameters.count as u32;
                    continue;
                }
            };
```

**File:** rs/sns/governance/src/governance.rs (L4411-4435)
```rust
    ///
    /// Preconditions:
    /// - The caller must be the Sale canister deployed along with this SNS Governance
    ///   canister.
    /// - Each NeuronRecipe's `stake_e8s` is at least neuron_minimum_stake_e8s
    ///   as defined in the `NervousSystemParameters`
    /// - Each NeuronRecipe's `followees` does not exceed max_followees_per_function
    ///   as defined in the `NervousSystemParameters`
    /// - There is available memory in the Governance canister for the newly created
    ///   Neuron.
    /// - The Neuron being claimed must not already exist in Governance.
    ///
    /// Claiming Neurons via this method differs from the primary
    /// `ManageNeuron::ClaimOrRefresh` way of creating neurons for governance. This
    /// method is only callable by the SNS Sale canister associated with this SNS
    /// Governance canister, and claims a batch of neurons instead of just one.
    /// As this is requested by the Sale canister which ensures the correct
    /// transfer of the tokens, this method does not check in the ledger. Additionally,
    /// the dissolve delay is set as part of the neuron creation process, while typically
    /// that is a separate command.
    pub fn claim_swap_neurons(
        &mut self,
        request: ClaimSwapNeuronsRequest,
        caller_principal_id: PrincipalId,
    ) -> ClaimSwapNeuronsResponse {
```
