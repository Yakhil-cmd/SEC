### Title
Unbounded `disburse_maturity_in_progress` Vec in SNS Governance — Missing Length Cap Allows O(N) Periodic Task Ledger Calls - (`rs/sns/governance/src/governance.rs`)

### Summary

The SNS governance `disburse_maturity` function pushes entries to `disburse_maturity_in_progress` without enforcing any maximum length. The NNS governance has an explicit `MAX_NUM_DISBURSEMENTS = 10` guard; the SNS governance has no equivalent. A neuron owner with `DisburseMaturity` permission and sufficient maturity can enqueue a large number of pending disbursements, causing O(N) ledger calls during every periodic task run.

### Finding Description

In `rs/sns/governance/src/governance.rs`, the `disburse_maturity` function performs the following checks before pushing a new entry:

- `percentage_to_disburse` is in `[1, 100]`
- `worst_case_maturity_modulation >= transaction_fee_e8s`

It then unconditionally pushes to the vec:

```rust
neuron
    .disburse_maturity_in_progress
    .push(disbursement_in_progress);
``` [1](#0-0) 

There is **no check on `disburse_maturity_in_progress.len()`** anywhere in the SNS governance code. [2](#0-1) 

By contrast, the NNS governance (`rs/nns/governance/src/governance/disburse_maturity.rs`) defines:

```rust
const MAX_NUM_DISBURSEMENTS: usize = 10;
```

and enforces it:

```rust
if num_disbursements >= MAX_NUM_DISBURSEMENTS {
    return Err(InitiateMaturityDisbursementError::TooManyDisbursements);
}
``` [3](#0-2) [4](#0-3) 

The SNS governance has no analogous constant or error variant.

### Impact Explanation

Each call with `percentage_to_disburse = 1` deducts 1% of remaining maturity. With a neuron starting at `u64::MAX` maturity (~1.8×10¹⁹ e8s) and a typical transaction fee of 10,000 e8s, the natural floor (worst-case modulation check) allows approximately **~3,000 entries** before calls start failing. Each `DisburseMaturityInProgress` entry carries `amount_e8s`, `timestamp_of_disbursement_seconds`, `account_to_disburse_to`, and `finalize_disbursement_timestamp_seconds`. [5](#0-4) 

During `maybe_finalize_disburse_maturity` (called from `run_periodic_tasks`), the canister iterates all entries and issues one ledger call per ready entry. With thousands of entries, this causes:

1. O(N) inter-canister ledger calls per periodic task invocation
2. Increased neuron proto serialization/deserialization cost on every heartbeat
3. Potential periodic task timeout, blocking other governance operations

### Likelihood Explanation

The attacker must legitimately hold a neuron with `DisburseMaturity` permission and large accumulated maturity. This is achievable through normal SNS participation. No privileged access, key compromise, or consensus attack is required. The attack is fully executable via the public `manage_neuron` ingress endpoint.

### Recommendation

Add an explicit cap on `disburse_maturity_in_progress.len()` in the SNS `disburse_maturity` function, mirroring the NNS governance pattern:

```rust
const MAX_DISBURSE_MATURITY_IN_PROGRESS: usize = 10;

if neuron.disburse_maturity_in_progress.len() >= MAX_DISBURSE_MATURITY_IN_PROGRESS {
    return Err(GovernanceError::new_with_message(
        ErrorType::PreconditionFailed,
        "Too many disbursements in progress for this neuron.",
    ));
}
```

This check should be applied before the push at line 1697.

### Proof of Concept

1. Create an SNS neuron with large `maturity_e8s_equivalent` (e.g., via reward accumulation or test setup).
2. Repeatedly call `manage_neuron(DisburseMaturity { percentage_to_disburse: 1, to_account: None })` as the neuron controller.
3. Observe that each call succeeds and `disburse_maturity_in_progress.len()` grows by 1 each time.
4. After ~3,000 calls (with max maturity), the vec reaches its natural floor limit.
5. Advance time past the 7-day disbursement delay and call `run_periodic_tasks`; observe O(N) ledger calls issued in a single periodic task execution.

The NNS governance test `test_initiate_maturity_disbursement_too_many_disbursements` confirms the NNS has this guard; no equivalent test exists in the SNS governance test suite. [6](#0-5)

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

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L38-40)
```rust
/// The maximum number of disbursements in a neuron. This makes it possible to do daily
/// disbursements after every reward event (as 10 > 7).
const MAX_NUM_DISBURSEMENTS: usize = 10;
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L306-308)
```rust
    if num_disbursements >= MAX_NUM_DISBURSEMENTS {
        return Err(InitiateMaturityDisbursementError::TooManyDisbursements);
    }
```

**File:** rs/sns/governance/src/gen/ic_sns_governance.pb.v1.rs (L81-92)
```rust
pub struct DisburseMaturityInProgress {
    /// This field is the quantity of maturity in e8s that has been decremented from a Neuron to
    /// be modulated and disbursed as SNS tokens.
    #[prost(uint64, tag = "1")]
    pub amount_e8s: u64,
    #[prost(uint64, tag = "2")]
    pub timestamp_of_disbursement_seconds: u64,
    #[prost(message, optional, tag = "3")]
    pub account_to_disburse_to: ::core::option::Option<Account>,
    #[prost(uint64, optional, tag = "4")]
    pub finalize_disbursement_timestamp_seconds: ::core::option::Option<u64>,
}
```

**File:** rs/nns/governance/src/governance/disburse_maturity_tests.rs (L404-438)
```rust
#[test]
fn test_initiate_maturity_disbursement_too_many_disbursements() {
    let mut neuron_store = NeuronStore::new(BTreeMap::new());
    let neuron = create_neuron_builder().build();
    neuron_store.add_neuron(neuron).unwrap();

    for _ in 0..10 {
        initiate_maturity_disbursement(
            &mut neuron_store,
            &CONTROLLER,
            &NeuronId { id: 1 },
            &DisburseMaturity {
                percentage_to_disburse: 1,
                to_account: None,
                to_account_identifier: None,
            },
            NOW_SECONDS,
        )
        .unwrap();
    }

    assert_eq!(
        initiate_maturity_disbursement(
            &mut neuron_store,
            &CONTROLLER,
            &NeuronId { id: 1 },
            &DisburseMaturity {
                percentage_to_disburse: 1,
                to_account: None,
                to_account_identifier: None,
            },
            NOW_SECONDS,
        ),
        Err(InitiateMaturityDisbursementError::TooManyDisbursements)
    );
```
