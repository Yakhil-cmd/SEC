### Title
SNS Swap Minimum-Participant SNS Token Validation Uses Direct-Only ICP Cap While Distribution Divides by Total (Direct + Neurons' Fund) ICP — (`rs/sns/init/src/lib.rs`)

### Summary

The SNS initialization validation computes the minimum SNS tokens a participant would receive using `max_direct_participation_icp_e8s` as the denominator. However, the actual token distribution at swap finalization divides by `total_participant_icp_e8s`, which includes **both** direct and Neurons' Fund (NF) participation. When NF participation is significant, every direct participant who contributes exactly `min_participant_icp_e8s` receives fewer SNS tokens than the validation assumed, potentially below `neuron_basket_count × (neuron_minimum_stake_e8s + transaction_fee_e8s)`. This causes neuron claiming to fail at finalization, leaving participants' SNS tokens permanently stranded in neuron subaccounts with no recovery path.

---

### Finding Description

**Validation (SNS init, `rs/sns/init/src/lib.rs`, lines 1636–1642):**

```rust
let min_participant_sns_e8s = min_participant_icp_e8s as u128
    * initial_swap_amount_e8s as u128
    / max_direct_participation_icp_e8s as u128;   // ← denominator: direct cap only

let min_participant_icp_e8s_big_enough = min_participant_sns_e8s
    >= neuron_basket_construction_parameters_count as u128
        * (neuron_minimum_stake_e8s + sns_transaction_fee_e8s) as u128;
``` [1](#0-0) 

The same floor-division pattern appears in the swap-open validation in `rs/sns/swap/src/types.rs`:

```rust
let min_participant_sns_e8s = self.min_participant_icp_e8s as u128
    * self.sns_token_e8s as u128
    / self.max_icp_e8s as u128;   // ← denominator: direct-only max_icp_e8s
``` [2](#0-1) 

**Actual distribution (`rs/sns/swap/src/swap.rs`, lines 848–852):**

```rust
let amount_sns_e8s = Swap::scale(
    buyer_state.amount_icp_e8s(),
    sns_being_offered_e8s,
    total_participant_icp_e8s,   // ← denominator: direct + NF combined
);
``` [3](#0-2) 

`total_participant_icp_e8s` is computed as:

```rust
pub fn current_total_participation_e8s(&self) -> u64 {
    let current_direct_participation_e8s = self.current_direct_participation_e8s();
    let current_neurons_fund_participation_e8s = self.current_neurons_fund_participation_e8s();
    current_direct_participation_e8s
        .checked_add(current_neurons_fund_participation_e8s)
        ...
}
``` [4](#0-3) 

The `scale` function itself uses integer floor division:

```rust
let r = (amount_icp_e8s as u128)
    .saturating_mul(total_sns_e8s as u128)
    .div(NonZeroU128::from(total_icp_e8s));
``` [5](#0-4) 

**The gap:** When NF participation is non-zero, `total_participant_icp_e8s > max_direct_participation_icp_e8s`. The validation checks:

```
floor(min_participant_icp_e8s × sns_token_e8s / max_direct_participation_icp_e8s) ≥ threshold
```

But the actual tokens received are:

```
floor(min_participant_icp_e8s × sns_token_e8s / (max_direct_participation_icp_e8s + nf_participation))
```

which is strictly less than the validated value whenever `nf_participation > 0`.

---

### Impact Explanation

A direct participant who contributes exactly `min_participant_icp_e8s` ICP receives fewer SNS tokens than the validation guaranteed. If the shortfall pushes their allocation below `neuron_basket_count × (neuron_minimum_stake_e8s + transaction_fee_e8s)`, the SNS governance canister will reject the neuron claim at finalization. The SNS tokens are already transferred to the neuron subaccount by `sweep_sns_tokens` before the claim attempt; there is no refund path in the swap canister once the swap is committed. Affected participants permanently lose their SNS tokens and receive no neurons.

The log line at line 985 acknowledges the "change" (undistributed tokens) but treats it as informational only — no corrective action is taken:

```rust
sns_being_offered_e8s - total_sns_tokens_sold_e8s
``` [6](#0-5) 

---

### Likelihood Explanation

NF participation is a standard feature of SNS swaps using matched funding. The NF participation amount is computed dynamically from the matched-funding polynomial and can be a substantial fraction of direct participation (up to 10% of total NF maturity). Any SNS whose parameters are set so that `min_participant_sns_e8s` is close to the neuron-minimum threshold — a common configuration to maximize participation breadth — will silently violate the guarantee for minimum-contributing participants whenever NF participation is active. No privileged access is required; the condition arises from normal swap operation.

---

### Recommendation

Replace the floor division in both validation sites with the same denominator used at distribution time, or use ceiling division in the validation so the check is conservative in the correct direction:

```rust
// In rs/sns/init/src/lib.rs and rs/sns/swap/src/types.rs:
// Use ceiling division so the check is strict:
let min_participant_sns_e8s = (min_participant_icp_e8s as u128
    * initial_swap_amount_e8s as u128
    + max_direct_participation_icp_e8s as u128 - 1)
    / max_direct_participation_icp_e8s as u128;
```

Additionally, the validation should account for the maximum possible NF participation by using `max_direct_participation_icp_e8s + max_neurons_fund_participation_icp_e8s` as the denominator, matching the worst-case `total_participant_icp_e8s` at distribution time.

---

### Proof of Concept

**Parameters (all in e8s):**
- `min_participant_icp_e8s` = 2 × 10⁸ (2 ICP)
- `sns_token_e8s` = 1,000,000 × 10⁸
- `max_direct_participation_icp_e8s` = 100 × 10⁸ (100 ICP)
- `neuron_basket_count` = 3
- `neuron_minimum_stake_e8s` = 3,000
- `sns_transaction_fee_e8s` = 1,000
- `max_neurons_fund_participation_icp_e8s` = 100 × 10⁸ (100 ICP, matched 1:1)

**Validation check (passes):**
```
min_participant_sns_e8s = floor(2e8 × 1e14 / 100e8) = 20,000
threshold = 3 × (3,000 + 1,000) = 12,000
20,000 ≥ 12,000 → PASSES
```

**Actual distribution when NF contributes 100 ICP:**
```
total_participant_icp_e8s = 100e8 (direct) + 100e8 (NF) = 200e8
actual_sns = floor(2e8 × 1e14 / 200e8) = 10,000
10,000 < 12,000 → neuron claim FAILS
```

The participant's 2 ICP is committed to the swap, 10,000 SNS e8s are transferred to their neuron subaccount, but no neuron is created. The tokens are permanently stranded. [7](#0-6) [8](#0-7) [9](#0-8)

### Citations

**File:** rs/sns/init/src/lib.rs (L1636-1655)
```rust
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

**File:** rs/sns/swap/src/types.rs (L346-348)
```rust
        let min_participant_sns_e8s = self.min_participant_icp_e8s as u128
            * self.sns_token_e8s as u128
            / self.max_icp_e8s as u128;
```

**File:** rs/sns/swap/src/swap.rs (L487-501)
```rust
    pub fn current_total_participation_e8s(&self) -> u64 {
        let current_direct_participation_e8s = self.current_direct_participation_e8s();
        let current_neurons_fund_participation_e8s = self.current_neurons_fund_participation_e8s();
        current_direct_participation_e8s
            .checked_add(current_neurons_fund_participation_e8s)
            .unwrap_or_else(|| {
                log!(
                    ERROR,
                    "current_direct_participation_e8s ({current_direct_participation_e8s}) \
                    + current_neurons_fund_participation_e8s ({current_neurons_fund_participation_e8s}) \
                    > u64::MAX",
                );
                u64::MAX
            })
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

**File:** rs/sns/swap/src/swap.rs (L983-986)
```rust
            total_sns_tokens_sold_e8s,
            sns_being_offered_e8s,
            sns_being_offered_e8s - total_sns_tokens_sold_e8s
        );
```
