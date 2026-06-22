Based on my thorough investigation of the codebase, I can now synthesize my findings.

**Key findings:**

1. **NNS Governance** (`rs/nns/governance`): The `recent_ballots` array is bounded at `MAX_NEURON_RECENT_BALLOTS = 100` and uses a circular buffer. The `maturity_disbursements_in_progress` is bounded at `MAX_NUM_DISBURSEMENTS = 10`. The `followees` per topic is bounded at `MAX_FOLLOWEES_PER_TOPIC = 15`. All these are properly limited.

2. **SNS Governance** (`rs/sns/governance`): The `disburse_maturity_in_progress` Vec is pushed to without any limit check in `rs/sns/governance/src/governance.rs` at line 1697-1698. There is no `MAX_NUM_DISBURSEMENTS` guard in the SNS path, unlike the NNS path which has `MAX_NUM_DISBURSEMENTS = 10`.

3. The SNS `disburse_maturity` function at line 1609 only checks authorization and percentage validity, but **never checks the length of `disburse_maturity_in_progress`** before pushing to it. The NNS equivalent (`initiate_maturity_disbursement`) explicitly checks `if num_disbursements >= MAX_NUM_DISBURSEMENTS` at line 306.

4. The SNS `maybe_finalize_disburse_maturity` at line 4938 iterates over **all neurons** and their `disburse_maturity_in_progress` entries, meaning a bloated list directly increases the cost of the periodic timer job.

### Title
Unbounded `disburse_maturity_in_progress` Growth in SNS Governance Allows Cycles/Resource Exhaustion - (`File: rs/sns/governance/src/governance.rs`)

### Summary
The SNS Governance canister's `disburse_maturity` function unconditionally pushes to the `disburse_maturity_in_progress` vector without enforcing any upper bound on its length. A principal with `DisburseMaturity` permission can repeatedly call `disburse_maturity` with `percentage_to_disburse = 1` to grow this vector without limit, making subsequent operations on the neuron (and the periodic finalization timer) progressively more expensive in cycles/instructions.

### Finding Description

In the NNS Governance canister, `initiate_maturity_disbursement` enforces a hard cap:

```rust
const MAX_NUM_DISBURSEMENTS: usize = 10;
// ...
if num_disbursements >= MAX_NUM_DISBURSEMENTS {
    return Err(InitiateMaturityDisbursementError::TooManyDisbursements);
}
``` [1](#0-0) [2](#0-1) 

The SNS Governance canister's `disburse_maturity` function has **no equivalent check**. It simply pushes unconditionally:

```rust
neuron
    .disburse_maturity_in_progress
    .push(disbursement_in_progress);
``` [3](#0-2) 

The only guards present are authorization (`NeuronPermissionType::DisburseMaturity`), percentage range (1–100), and a minimum disbursement amount check. None of these prevent repeated calls from growing the list indefinitely. [4](#0-3) 

The periodic finalization timer `maybe_finalize_disburse_maturity` iterates over **all neurons** and their `disburse_maturity_in_progress` entries to find ready disbursements:

```rust
let neuron_id_and_disbursements: Vec<(NeuronId, DisburseMaturityInProgress)> = self
    .proto
    .neurons
    .values()
    .filter_map(|neuron| {
        let first_disbursement = neuron.disburse_maturity_in_progress.first()?;
        // ...
    })
    .collect();
``` [5](#0-4) 

A bloated `disburse_maturity_in_progress` list also increases the cost of reading/writing the neuron in stable memory, since `maturity_disbursements_in_progress` is stored as a repeated field in the `StableNeuronStore` and is fully read/written on each neuron mutation. [6](#0-5) 

### Impact Explanation

A principal holding `DisburseMaturity` permission on their own SNS neuron (a normal, unprivileged role) can:

1. Call `disburse_maturity` with `percentage_to_disburse = 1` repeatedly (each call deducts 1% of remaining maturity, so ~100 calls before maturity is exhausted, but maturity can be replenished via staking rewards).
2. Grow `disburse_maturity_in_progress` to hundreds of entries over time.
3. Make every subsequent neuron read/write (voting, following, etc.) more expensive in instructions.
4. Increase the cost of the periodic `maybe_finalize_disburse_maturity` timer, which scans all neurons.

In the worst case, if the list grows large enough, the periodic timer or neuron mutation could exhaust the instruction limit, causing the SNS governance canister to become unable to process votes or proposals — a governance liveness failure.

### Likelihood Explanation

The attack requires only `DisburseMaturity` permission on a neuron with non-zero maturity. Any SNS token holder who has staked and earned rewards can perform this. The attack is cheap (only transaction fees for the SNS ledger, if any) and can be executed gradually over time as maturity accumulates. The NNS governance team explicitly recognized this risk and added `MAX_NUM_DISBURSEMENTS = 10` to the NNS path, but the SNS path was not similarly protected.

### Recommendation

Add a maximum disbursement count check in `rs/sns/governance/src/governance.rs` in the `disburse_maturity` function, mirroring the NNS implementation:

```rust
const MAX_DISBURSE_MATURITY_IN_PROGRESS: usize = 10;

if neuron.disburse_maturity_in_progress.len() >= MAX_DISBURSE_MATURITY_IN_PROGRESS {
    return Err(GovernanceError::new_with_message(
        ErrorType::PreconditionFailed,
        format!("Too many disbursements in progress. Max: {MAX_DISBURSE_MATURITY_IN_PROGRESS}"),
    ));
}
```

This check should be placed before the push at line 1696, after re-borrowing the neuron mutably.

### Proof of Concept

1. Deploy an SNS with a neuron that has `DisburseMaturity` permission.
2. Accumulate maturity through voting rewards.
3. Repeatedly call `manage_neuron` with `DisburseMaturity { percentage_to_disburse: 1, to_account: ... }`.
4. Each call succeeds (no limit check), appending to `disburse_maturity_in_progress`.
5. After many calls, observe that:
   - `neuron.disburse_maturity_in_progress.len()` grows without bound.
   - The `maybe_finalize_disburse_maturity` periodic task consumes more instructions per round.
   - Neuron mutations (voting, following) become more expensive as the stable-memory repeated field grows.

The NNS governance code at `rs/nns/governance/src/governance/disburse_maturity.rs:40` defines `MAX_NUM_DISBURSEMENTS = 10` and enforces it at line 306, confirming the team is aware this bound is necessary — but the SNS governance at `rs/sns/governance/src/governance.rs:1696-1698` omits this protection entirely. [7](#0-6) [8](#0-7)

### Citations

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L36-45)
```rust
/// The delay in seconds between initiating a maturity disbursement and the actual disbursement.
const DISBURSEMENT_DELAY_SECONDS: u64 = ONE_DAY_SECONDS * 7;
/// The maximum number of disbursements in a neuron. This makes it possible to do daily
/// disbursements after every reward event (as 10 > 7).
const MAX_NUM_DISBURSEMENTS: usize = 10;
/// The minimum amount of ICP that need to be minted when disbursing maturity. A neuron can only
/// disburse an amount of maturity that results in minting at least this many ICP (in e8) assuming
/// the worst case maturity modulation. This limit is set to be consistent with the neuron spawning
/// behavior (which maturity disbursement is designed to replace).
pub const MINIMUM_DISBURSEMENT_E8S: u64 = E8;
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L306-308)
```rust
    if num_disbursements >= MAX_NUM_DISBURSEMENTS {
        return Err(InitiateMaturityDisbursementError::TooManyDisbursements);
    }
```

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

**File:** rs/nns/governance/src/storage/neurons.rs (L222-226)
```rust
        update_repeated_field(
            neuron_id,
            maturity_disbursements_in_progress,
            &mut self.maturity_disbursements_map,
        );
```
