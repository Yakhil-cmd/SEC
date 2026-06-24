### Title
Silent Rounding-to-Zero in `stake_maturity_of_neuron` Allows Maturity to Remain Unstaked Without Error - (`rs/sns/governance/src/governance.rs`)

---

### Summary

In SNS governance's `stake_maturity_of_neuron`, the proportional maturity calculation uses integer division that can silently truncate to zero. Unlike the analogous `merge_maturity` and `disburse_maturity` operations — which both enforce a minimum-amount guard — `stake_maturity_of_neuron` has no such guard. When the computed amount is zero, the function returns `Ok(...)` with no state change: the neuron's `maturity_e8s_equivalent` is not reduced and `staked_maturity_e8s_equivalent` is not increased.

---

### Finding Description

In `rs/sns/governance/src/governance.rs`, `stake_maturity_of_neuron` computes the amount to stake as:

```rust
let mut maturity_to_stake = (neuron
    .maturity_e8s_equivalent
    .saturating_mul(percentage_to_stake as u64))
    / 100;
``` [1](#0-0) 

When `maturity_e8s_equivalent * percentage_to_stake < 100`, Rust integer division truncates the result to `0`. The function then proceeds to:

```rust
neuron.maturity_e8s_equivalent = neuron
    .maturity_e8s_equivalent
    .saturating_sub(maturity_to_stake);   // subtracts 0 — no change

neuron.staked_maturity_e8s_equivalent = Some(
    neuron.staked_maturity_e8s_equivalent
        .unwrap_or(0)
        .saturating_add(maturity_to_stake),  // adds 0 — no change
);
``` [2](#0-1) 

The function returns `Ok(StakeMaturityResponse { ... })` with the unchanged values, giving the caller no indication that nothing happened.

**Contrast with guarded operations.** Both `merge_maturity` and `disburse_maturity` reject zero-amount results before mutating state:

- `merge_maturity` checks `if maturity_to_merge <= transaction_fee_e8s { return Err(...) }` [3](#0-2) 
- NNS `initiate_maturity_disbursement` checks `if disbursement_maturity_e8s < MINIMUM_DISBURSEMENT_E8S { return Err(...) }` [4](#0-3) 

`stake_maturity_of_neuron` has no equivalent guard. [5](#0-4) 

**Accumulation pattern.** A neuron controller who repeatedly calls `StakeMaturity` with a small `percentage_to_stake` (e.g., `1`) while the neuron's `maturity_e8s_equivalent` is below 100 e8s will receive repeated `Ok` responses while the neuron's staked maturity never increases. The neuron's liquid maturity remains higher than the user expects, and staked maturity (which contributes to voting power) remains lower than expected — a silent, accumulating divergence between intended and actual state.

---

### Impact Explanation

- **Voting power lower than intended.** `staked_maturity_e8s_equivalent` contributes to a neuron's voting power in SNS governance. If staking silently no-ops, the neuron's effective voting power is lower than the controller believes.
- **Silent success misleads callers.** The `Ok(StakeMaturityResponse)` return value reports the current (unchanged) maturity values, which a caller may interpret as confirmation that staking occurred.
- **Maturity accounting drift.** Over repeated calls the gap between the user's mental model and actual on-chain state grows, analogous to the locked-balance drift described in the reference report.

---

### Likelihood Explanation

The condition `maturity_e8s_equivalent * percentage_to_stake < 100` is triggered when a neuron holds fewer than `100 / percentage_to_stake` e8s of maturity. For `percentage_to_stake = 1` this is fewer than 100 e8s (≈ 0.000001 SNS tokens). Neurons with very small accumulated maturity — common early in an SNS's life or after many partial disbursements — can reach this state. Any authorized neuron controller (an unprivileged ingress sender) can trigger this path by calling `ManageNeuron::StakeMaturity`.

---

### Recommendation

Add a minimum-amount guard immediately after computing `maturity_to_stake`, consistent with the pattern used in `merge_maturity` and `initiate_maturity_disbursement`:

```rust
if maturity_to_stake == 0 {
    return Err(GovernanceError::new_with_message(
        ErrorType::PreconditionFailed,
        "The requested percentage of maturity rounds to zero e8s; \
         increase the percentage or accumulate more maturity first.",
    ));
}
```

Alternatively, enforce a protocol-level minimum (e.g., `>= transaction_fee_e8s`) as `merge_maturity` does.

---

### Proof of Concept

```
neuron.maturity_e8s_equivalent = 99   // 99 e8s of liquid maturity
percentage_to_stake = 1               // caller requests 1%

maturity_to_stake = (99 * 1) / 100   // = 0  (integer truncation)

// State after the call — unchanged:
neuron.maturity_e8s_equivalent        = 99   // should be 99
neuron.staked_maturity_e8s_equivalent = 0    // should be 0 (no change)

// Return value signals success:
Ok(StakeMaturityResponse { maturity_e8s: 99, staked_maturity_e8s: 0 })
```

The caller receives `Ok` with no indication that the requested staking did not occur. Repeating this call any number of times produces the same silent no-op, while the neuron's voting power remains lower than the controller intends.

### Citations

**File:** rs/sns/governance/src/governance.rs (L1483-1490)
```rust
        if maturity_to_merge <= transaction_fee_e8s {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!(
                    "Tried to merge {maturity_to_merge} e8s, but can't merge an amount less than the transaction fee of {transaction_fee_e8s} e8s"
                ),
            ));
        }
```

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

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L293-298)
```rust
    if disbursement_maturity_e8s < MINIMUM_DISBURSEMENT_E8S {
        return Err(InitiateMaturityDisbursementError::DisbursementTooSmall {
            disbursement_maturity_e8s,
            minimum_disbursement_e8s: MINIMUM_DISBURSEMENT_E8S,
        });
    }
```
