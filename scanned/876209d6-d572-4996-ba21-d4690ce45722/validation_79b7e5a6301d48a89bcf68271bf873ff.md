### Title
Unbounded `disburse_maturity_in_progress` Vector Growth in SNS Governance Causes Cycles/Resource Exhaustion - (File: `rs/sns/governance/src/governance.rs`)

### Summary
The SNS governance canister's `disburse_maturity` function appends to the `disburse_maturity_in_progress` vector on a neuron without enforcing any upper bound on the number of concurrent entries. An authorized neuron controller can repeatedly call `DisburseMaturity` with small percentages, accumulating an arbitrarily large number of pending disbursement entries. This causes unbounded growth of the neuron's serialized state and increases instruction consumption for every subsequent neuron read/write and for the finalization timer, analogous to the MultiFee Distribution `userLocks`/`userEarnings` array bloat.

### Finding Description

In `rs/sns/governance/src/governance.rs`, the `disburse_maturity` function unconditionally pushes a new `DisburseMaturityInProgress` entry onto the neuron's `disburse_maturity_in_progress` vector:

```rust
// rs/sns/governance/src/governance.rs ~line 1696-1698
neuron
    .disburse_maturity_in_progress
    .push(disbursement_in_progress);
```

There is no check on the current length of `disburse_maturity_in_progress` before this push. [1](#0-0) 

By contrast, the NNS governance equivalent (`initiate_maturity_disbursement`) explicitly enforces `MAX_NUM_DISBURSEMENTS = 10`:

```rust
// rs/nns/governance/src/governance/disburse_maturity.rs
const MAX_NUM_DISBURSEMENTS: usize = 10;
...
if num_disbursements >= MAX_NUM_DISBURSEMENTS {
    return Err(InitiateMaturityDisbursementError::TooManyDisbursements);
}
``` [2](#0-1) [3](#0-2) 

The SNS neuron's `disburse_maturity_in_progress` field is a `repeated` protobuf field stored inline in the neuron's serialized state: [4](#0-3) 

The finalization timer (`finalize_disburse_maturity`) iterates over all neurons with pending disbursements and calls `remove(0)` on the vector after each successful transfer: [5](#0-4) 

### Impact Explanation

An authorized neuron controller (or any principal with `DisburseMaturity` permission) can call `manage_neuron { DisburseMaturity { percentage_to_disburse: 1 } }` up to ~100 times per neuron (each call deducts 1% of remaining maturity, bounded by the minimum viable disbursement check). With sufficient maturity, this creates a large `disburse_maturity_in_progress` vector. The consequences are:

1. **Increased instruction cost per neuron operation**: Every read or write of the neuron deserializes/serializes the full `disburse_maturity_in_progress` vector. A bloated vector increases the instruction cost of all neuron operations for that neuron.
2. **Finalization timer degradation**: The `finalize_disburse_maturity` periodic task iterates over neurons with pending disbursements. A neuron with many pending entries causes repeated expensive neuron mutations (each `remove(0)` on a large vector is O(n)), potentially exhausting the instruction limit for the timer execution and causing it to trap or fail to make progress.
3. **Canister state bloat**: The SNS governance canister's heap grows proportionally to the total number of pending disbursement entries across all neurons, consuming memory resources.

### Likelihood Explanation

Any SNS neuron controller with sufficient maturity (earned through voting rewards) can trigger this. The `DisburseMaturity` command is a standard user-facing operation reachable via ingress. No privileged access is required. The minimum disbursement check (`worst_case_maturity_modulation >= transaction_fee_e8s`) limits the rate but does not prevent accumulation over time as maturity accrues from rewards. This is a realistic attack for any SNS with active voting rewards.

### Recommendation

Add an upper bound check on `disburse_maturity_in_progress.len()` in `Governance::disburse_maturity` before pushing, mirroring the NNS governance pattern:

```rust
const MAX_DISBURSE_MATURITY_IN_PROGRESS: usize = 10;

if neuron.disburse_maturity_in_progress.len() >= MAX_DISBURSE_MATURITY_IN_PROGRESS {
    return Err(GovernanceError::new_with_message(
        ErrorType::PreconditionFailed,
        "Too many maturity disbursements in progress.",
    ));
}
```

### Proof of Concept

1. Create an SNS neuron with sufficient maturity (e.g., 1000 SNS tokens worth of maturity).
2. As the neuron controller, call `manage_neuron` with `DisburseMaturity { percentage_to_disburse: 1 }` repeatedly (up to ~100 times, limited by maturity depletion).
3. Observe that `neuron.disburse_maturity_in_progress.len()` grows with each call, with no rejection.
4. After accumulating many entries, observe that subsequent neuron operations (e.g., `get_neuron`, `manage_neuron`) consume more instructions due to the larger serialized neuron state.
5. Observe that the `finalize_disburse_maturity` periodic task takes proportionally more instructions to process this neuron, potentially failing to complete within the instruction limit if many neurons are similarly bloated.

The root cause is the missing length guard at `rs/sns/governance/src/governance.rs` line ~1696, contrasted with the NNS governance fix at `rs/nns/governance/src/governance/disburse_maturity.rs` line 306. [6](#0-5)

### Citations

**File:** rs/sns/governance/src/governance.rs (L1609-1698)
```rust
    pub fn disburse_maturity(
        &mut self,
        id: &NeuronId,
        caller: &PrincipalId,
        disburse_maturity: &DisburseMaturity,
    ) -> Result<DisburseMaturityResponse, GovernanceError> {
        let neuron = self.get_neuron_result(id)?;
        neuron.check_authorized(caller, NeuronPermissionType::DisburseMaturity)?;

        // If no account was provided, transfer to the caller's account.
        let to_account: Account = match disburse_maturity.to_account.as_ref() {
            None => Account {
                owner: caller.0,
                subaccount: None,
            },
            Some(account) => Account::try_from(account.clone()).map_err(|e| {
                GovernanceError::new_with_message(
                    ErrorType::InvalidCommand,
                    format!("The given account to disburse the maturity to is invalid due to: {e}"),
                )
            })?,
        };
        let to_account_proto: AccountProto = AccountProto::from(to_account);

        if disburse_maturity.percentage_to_disburse > 100
            || disburse_maturity.percentage_to_disburse == 0
        {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "The percentage of maturity to disburse must be a value between 1 and 100 (inclusive).",
            ));
        }

        // The amount to deduct = the amount in the neuron * request.percentage / 100.
        let maturity_to_deduct = neuron
            .maturity_e8s_equivalent
            .checked_mul(disburse_maturity.percentage_to_disburse as u64)
            .expect("Overflow while processing maturity to disburse.")
            .checked_div(100)
            .expect("Error when processing maturity to disburse.")
            as u128;

        let maturity_to_deduct = maturity_to_deduct as u64;

        let transaction_fee_e8s = self.transaction_fee_e8s_or_panic();
        let worst_case_maturity_modulation =
            apply_maturity_modulation(maturity_to_deduct, MIN_MATURITY_MODULATION_PERMYRIAD)
                // Applying maturity modulation is a safe operation.
                // However, in the case that the method fails to apply the equation, return an
                // error instead of throwing a panic.
                .map_err(|err| {
                    GovernanceError::new_with_message(
                        ErrorType::PreconditionFailed,
                        format!(
                            "Could not calculate worst case maturity modulation \
                            and therefore cannot disburse maturity. Err: {err}"
                        ),
                    )
                })?;

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

        let now_seconds = self.env.now();
        let disbursement_in_progress = DisburseMaturityInProgress {
            amount_e8s: maturity_to_deduct,
            timestamp_of_disbursement_seconds: now_seconds,
            account_to_disburse_to: Some(to_account_proto),
            finalize_disbursement_timestamp_seconds: Some(
                now_seconds + MATURITY_DISBURSEMENT_DELAY_SECONDS,
            ),
        };

        // Re-borrow the neuron mutably to update now that the maturity has been
        // deducted and is waiting until the end of the window to modulate and disburse.
        let neuron = self.get_neuron_result_mut(id)?;
        neuron.maturity_e8s_equivalent = neuron
            .maturity_e8s_equivalent
            .saturating_sub(maturity_to_deduct);
        neuron
            .disburse_maturity_in_progress
            .push(disbursement_in_progress);
```

**File:** rs/sns/governance/src/governance.rs (L5069-5069)
```rust
                    neuron.disburse_maturity_in_progress.remove(0);
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L40-40)
```rust
const MAX_NUM_DISBURSEMENTS: usize = 10;
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L306-308)
```rust
    if num_disbursements >= MAX_NUM_DISBURSEMENTS {
        return Err(InitiateMaturityDisbursementError::TooManyDisbursements);
    }
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L236-240)
```text
  // Disburse maturity operations that are currently underway.
  // The entries are sorted by `timestamp_of_disbursement_seconds`-values,
  // with the oldest entries first, i.e. it holds for all i that:
  // entry[i].timestamp_of_disbursement_seconds <= entry[i+1].timestamp_of_disbursement_seconds
  repeated DisburseMaturityInProgress disburse_maturity_in_progress = 18;
```
