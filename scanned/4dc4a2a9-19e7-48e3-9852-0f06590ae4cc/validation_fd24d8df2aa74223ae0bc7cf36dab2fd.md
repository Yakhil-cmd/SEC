### Title
Zero-Amount Maturity Staking via Integer Division Rounding - (File: rs/sns/governance/src/governance.rs)

### Summary
In the SNS governance canister, `stake_maturity_of_neuron` computes the amount to stake via integer division `(maturity_e8s_equivalent * percentage_to_stake) / 100`. When the neuron's maturity is small enough that this expression truncates to zero, the function succeeds and records a no-op state change without rejecting the call. This is a direct analog to the Lido "burn zero shares" rounding bug.

### Finding Description
The `stake_maturity_of_neuron` function in the SNS governance canister computes the amount of maturity to stake as:

```rust
let mut maturity_to_stake = (neuron
    .maturity_e8s_equivalent
    .saturating_mul(percentage_to_stake as u64))
    / 100;
``` [1](#0-0) 

After this computation, there is no guard checking that `maturity_to_stake > 0`. The function proceeds to subtract zero from `maturity_e8s_equivalent` and add zero to `staked_maturity_e8s_equivalent`, returning `Ok(StakeMaturityResponse {...})` with both fields reflecting the unchanged state. [2](#0-1) 

The only guard present is a range check on `percentage_to_stake` (must be 1–100), but no check that the resulting computed amount is non-zero. [3](#0-2) 

The identical pattern exists in the NNS governance canister's `stake_maturity_of_neuron`: [4](#0-3) 

By contrast, the SNS `disburse_maturity` function is partially protected because it checks `worst_case_maturity_modulation < transaction_fee_e8s` — but only when `transaction_fee_e8s > 0`. If the SNS is configured with a zero transaction fee, a zero-amount disbursement also passes through. [5](#0-4) 

### Impact Explanation
An authorized neuron controller can invoke `manage_neuron` → `StakeMaturity` on a neuron whose `maturity_e8s_equivalent` is small (e.g., 1–99 e8s) with `percentage_to_stake = 1`. The integer division `(99 * 1) / 100 = 0` produces a zero-amount stake. The call succeeds, emitting a `StakeMaturityResponse` that reports no change, while the neuron's maturity and staked-maturity fields are unmodified. This violates the invariant that a successful `StakeMaturity` call must move a positive amount of maturity into staked maturity. Integrators or front-ends relying on the success response to confirm a state change will be silently misled.

### Likelihood Explanation
Any SNS neuron controller whose neuron has accumulated a small amount of maturity (fewer than 100 e8s, which is plausible early in an SNS lifecycle or after many partial disbursements) can trigger this with a low `percentage_to_stake`. The call is a standard ingress message requiring no special privileges beyond neuron ownership.

### Recommendation
Add an explicit zero-amount guard immediately after computing `maturity_to_stake` in both `rs/sns/governance/src/governance.rs` and `rs/nns/governance/src/governance.rs`:

```rust
if maturity_to_stake == 0 {
    return Err(GovernanceError::new_with_message(
        ErrorType::PreconditionFailed,
        "The computed amount of maturity to stake is zero due to rounding. \
         Increase the percentage or accumulate more maturity before staking.",
    ));
}
```

The same guard should be applied in `disburse_maturity` after computing `maturity_to_deduct`, independent of the transaction-fee check.

### Proof of Concept
1. Create an SNS neuron with `maturity_e8s_equivalent = 50`.
2. Call `manage_neuron` with `Command::StakeMaturity(StakeMaturity { percentage_to_stake: Some(1) })`.
3. Computation: `(50 * 1) / 100 = 0`.
4. No error is returned; `StakeMaturityResponse { maturity_e8s: 50, staked_maturity_e8s: 0 }` is returned (unchanged state).
5. The neuron's `maturity_e8s_equivalent` remains 50 and `staked_maturity_e8s_equivalent` remains 0 — the call succeeded but performed no action. [6](#0-5)

### Citations

**File:** rs/sns/governance/src/governance.rs (L1540-1592)
```rust
    pub fn stake_maturity_of_neuron(
        &mut self,
        id: &NeuronId,
        caller: &PrincipalId,
        stake_maturity: &manage_neuron::StakeMaturity,
    ) -> Result<StakeMaturityResponse, GovernanceError> {
        let neuron = self.get_neuron_result(id)?.clone();

        let nid = neuron.id.as_ref().expect("Neurons must have an id");

        if !neuron.is_authorized(caller, NeuronPermissionType::StakeMaturity) {
            return Err(GovernanceError::new(ErrorType::NotAuthorized));
        }

        let percentage_to_stake = stake_maturity.percentage_to_stake.unwrap_or(100);

        if percentage_to_stake > 100 || percentage_to_stake == 0 {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "The percentage of maturity to stake must be a value between 0 (exclusive) and 100 (inclusive).",
            ));
        }

        let mut maturity_to_stake = (neuron
            .maturity_e8s_equivalent
            .saturating_mul(percentage_to_stake as u64))
            / 100;

        if maturity_to_stake > neuron.maturity_e8s_equivalent {
            maturity_to_stake = neuron.maturity_e8s_equivalent;
        }

        // Adjust the maturity of the neuron
        let neuron = self
            .get_neuron_result_mut(nid)
            .expect("Expected the neuron to exist");

        neuron.maturity_e8s_equivalent = neuron
            .maturity_e8s_equivalent
            .saturating_sub(maturity_to_stake);

        neuron.staked_maturity_e8s_equivalent = Some(
            neuron
                .staked_maturity_e8s_equivalent
                .unwrap_or(0)
                .saturating_add(maturity_to_stake),
        );

        Ok(StakeMaturityResponse {
            maturity_e8s: neuron.maturity_e8s_equivalent,
            staked_maturity_e8s: neuron.staked_maturity_e8s_equivalent.unwrap_or(0),
        })
    }
```

**File:** rs/sns/governance/src/governance.rs (L1669-1678)
```rust
        if worst_case_maturity_modulation < transaction_fee_e8s {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!(
                    "If worst case maturity modulation is applied (-5%) then this neuron would \
                     disburse {worst_case_maturity_modulation} e8s, but can't disburse an amount less than the transaction fee \
                     of {transaction_fee_e8s} e8s."
                ),
            ));
        }
```

**File:** rs/nns/governance/src/governance.rs (L2795-2796)
```rust
        let mut maturity_to_stake =
            (neuron_maturity_e8s_equivalent.saturating_mul(percentage_to_stake as u64)) / 100;
```
