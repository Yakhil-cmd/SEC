### Title
Unbounded `disburse_maturity_in_progress` Queue Per SNS Neuron Causes Indefinitely Delayed Maturity Disbursements - (File: `rs/sns/governance/src/governance.rs`)

### Summary

SNS governance's `disburse_maturity` imposes no upper bound on the number of entries in a neuron's `disburse_maturity_in_progress` list. The periodic timer `maybe_finalize_disburse_maturity` processes only the **first** entry per neuron per invocation. An authorized neuron controller can therefore queue hundreds of disbursements on a single neuron, causing all but the first to be indefinitely delayed while their maturity has already been deducted. NNS governance contains an explicit `MAX_NUM_DISBURSEMENTS = 10` guard that prevents this exact pattern; SNS governance has no equivalent.

### Finding Description

`Governance::disburse_maturity` in SNS governance accepts any number of successive calls from the neuron controller. Each call:

1. Validates the percentage (1–100) and that the worst-case post-modulation amount exceeds the transaction fee.
2. Deducts `maturity_to_deduct` from `neuron.maturity_e8s_equivalent` immediately.
3. Appends a new `DisburseMaturityInProgress` entry to `neuron.disburse_maturity_in_progress` with no length check. [1](#0-0) 

The periodic timer `maybe_finalize_disburse_maturity` then iterates over every neuron in `self.proto.neurons` and, for each, inspects only `disburse_maturity_in_progress.first()`: [2](#0-1) 

Only one disbursement per neuron is processed per timer invocation. After a successful transfer, `remove(0)` is called, advancing the queue by one: [3](#0-2) 

By contrast, NNS governance defines and enforces a hard cap: [4](#0-3) [5](#0-4) 

No analogous constant or check exists anywhere in the SNS governance source: [6](#0-5) 

### Impact Explanation

A neuron controller calls `disburse_maturity` repeatedly with `percentage_to_disburse = 1`. Each call deducts 1 % of the remaining maturity and enqueues a new disbursement. With 1 ICP-equivalent of maturity and a transaction fee of ~10 000 e8s, the geometric series allows roughly **450+ successive calls** before the per-call amount falls below the minimum. All deducted maturity is immediately removed from the neuron but sits in the queue. Because the timer processes exactly one entry per neuron per run, the N-th disbursement is not executed until N consecutive timer invocations have completed — each separated by the timer's scheduling interval. The user's maturity is therefore effectively locked in an ever-growing queue with no protocol-enforced upper bound on wait time. Additionally, the synchronous collection phase of `maybe_finalize_disburse_maturity` iterates over the entire neuron map on every invocation; a large number of neurons each carrying a long queue amplifies the per-invocation instruction cost of that phase.

### Likelihood Explanation

The attack requires only that the caller hold `NeuronPermissionType::DisburseMaturity` on a neuron with sufficient accumulated maturity — a normal, unprivileged user action. No special role, key, or governance majority is needed. The minimum maturity required to create a meaningful queue (tens of disbursements) is modest (a few ICP-equivalent). The call is a standard `manage_neuron` ingress message reachable by any SNS token holder.

### Recommendation

Add a constant analogous to NNS governance's `MAX_NUM_DISBURSEMENTS` in SNS governance and reject `disburse_maturity` calls when `neuron.disburse_maturity_in_progress.len() >= MAX_NUM_DISBURSEMENTS_SNS`. A value of 7–10 is consistent with the 7-day disbursement delay and the NNS precedent.

### Proof of Concept

1. Acquire an SNS neuron with ≥ 1 ICP-equivalent of accumulated maturity.
2. Send ~450 successive `manage_neuron { DisburseMaturity { percentage_to_disburse: 1, … } }` ingress messages. Each succeeds; each deducts maturity and appends to `disburse_maturity_in_progress`.
3. After 7 days, observe that `maybe_finalize_disburse_maturity` processes only the first entry per timer tick. The remaining ~449 disbursements are processed one-per-tick, meaning the last disbursement is not executed until ~449 additional timer invocations after the 7-day window opens.
4. Confirm that NNS governance rejects the same sequence at the 11th call with `TooManyDisbursements` (enforced at `rs/nns/governance/src/governance/disburse_maturity.rs:306`), while SNS governance accepts all calls indefinitely. [7](#0-6) [4](#0-3) [5](#0-4)

### Citations

**File:** rs/sns/governance/src/governance.rs (L1609-1706)
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

        Ok(DisburseMaturityResponse {
            // We still populate this field even though it's deprecated, since we cannot remove
            // required fields yet.
            amount_disbursed_e8s: maturity_to_deduct,
            amount_deducted_e8s: Some(maturity_to_deduct),
        })
    }
```

**File:** rs/sns/governance/src/governance.rs (L4938-4975)
```rust
        let neuron_id_and_disbursements: Vec<(NeuronId, DisburseMaturityInProgress)> = self
            .proto
            .neurons
            .values()
            .filter_map(|neuron| {
                let id = match neuron.id.as_ref() {
                    Some(id) => id,
                    None => {
                        log!(
                            ERROR,
                            "NeuronId is not set for neuron. This should never happen. \
                             Cannot disburse."
                        );
                        return None;
                    }
                };
                // The first entry is the oldest one, check whether it can be completed.
                let first_disbursement = neuron.disburse_maturity_in_progress.first()?;
                let finalize_disbursement_timestamp_seconds =
                    match first_disbursement.finalize_disbursement_timestamp_seconds {
                        Some(finalize_disbursement_timestamp_seconds) => {
                            finalize_disbursement_timestamp_seconds
                        }
                        None => {
                            log!(
                                ERROR,
                                "Finalize disbursement timestamp is not set. Cannot disburse."
                            );
                            return None;
                        }
                    };
                if now_seconds >= finalize_disbursement_timestamp_seconds {
                    Some((id.clone(), first_disbursement.clone()))
                } else {
                    None
                }
            })
            .collect();
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
