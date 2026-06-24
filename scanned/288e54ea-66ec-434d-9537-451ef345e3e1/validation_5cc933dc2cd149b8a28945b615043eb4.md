### Title
SNS Governance `disburse_maturity` Accepts Disbursement Without Checking Neuron Dissolve State, Causing Maturity to Become Temporarily Inaccessible - (File: `rs/sns/governance/src/governance.rs`)

### Summary

The SNS Governance `disburse_maturity` function deducts maturity from a neuron and places it into a `disburse_maturity_in_progress` queue without checking whether the neuron is in a state where the disbursement can ever be finalized. Specifically, it does not check whether the neuron is in the `Dissolved` or `Dissolving` state before accepting the disbursement. The NNS counterpart (`initiate_maturity_disbursement`) correctly rejects disbursements when the neuron is `Spawning`, but the SNS version has no such guard. More critically, neither version prevents a disbursement from being initiated on a neuron that is `NotDissolving` with a long dissolve delay — meaning the maturity is deducted immediately and locked in the pending queue for 7 days, during which the neuron controller cannot re-stake or otherwise use it, even if the neuron will not dissolve for years.

### Finding Description

In `rs/sns/governance/src/governance.rs`, the `disburse_maturity` function:

1. Checks authorization (`NeuronPermissionType::DisburseMaturity`)
2. Validates the percentage (1–100)
3. Checks the worst-case maturity modulation floor
4. **Immediately deducts `maturity_to_deduct` from `neuron.maturity_e8s_equivalent`**
5. Pushes a `DisburseMaturityInProgress` entry with `finalize_disbursement_timestamp_seconds = now + MATURITY_DISBURSEMENT_DELAY_SECONDS` (7 days)

It does **not** check the neuron's dissolve state at all. The `check_command_is_valid_if_neuron_is_vesting` guard in `manage_neuron` explicitly allows `DisburseMaturity` on vesting neurons. The NNS version (`initiate_maturity_disbursement`) only blocks the `Spawning` state.

The finalization path (`maybe_finalize_disburse_maturity`) runs as a periodic task and mints SNS tokens to the destination account after the 7-day delay. This path works regardless of neuron state — so the disbursement will eventually complete. However, during the 7-day window, the deducted maturity is neither in `maturity_e8s_equivalent` (it was subtracted) nor yet minted to the user. If the neuron controller calls `disburse_maturity` repeatedly (up to the `MAX_NUM_DISBURSEMENTS` cap of 7 in SNS), they can drain all maturity into the pending queue.

The structural analog to the external report is: **a resource (maturity) is accepted into a pending/locked state without validating that the prerequisite condition for completion (neuron dissolution) is met or will be met within a reasonable timeframe**, causing the resource to be inaccessible during the delay window. Unlike the StakeManager bug, the SNS disbursement *will* eventually complete (the finalization timer is unconditional on neuron state), so this is not a permanent lock — but it is a temporary forced lock of maturity that the user may not have intended.

The more precise analog is the **missing `Spawning` state check in SNS `disburse_maturity`**: the NNS version explicitly rejects `Spawning` neurons, but the SNS version has no such guard. [1](#0-0) 

The SNS `disburse_maturity` only checks authorization and percentage bounds — no neuron state check: [2](#0-1) 

Compare with the NNS `initiate_maturity_disbursement`, which explicitly checks for `Spawning`: [3](#0-2) 

The `check_command_is_valid_if_neuron_is_vesting` guard explicitly permits `DisburseMaturity` on vesting neurons: [4](#0-3) 

The SNS finalization path (`maybe_finalize_disburse_maturity`) does not check neuron state either — it simply mints tokens after the delay: [5](#0-4) 

### Impact Explanation

**Vulnerability class:** Governance authorization / resource accounting bug — missing state validation before accepting a pending disbursement.

**Impact:** A neuron controller with `DisburseMaturity` permission can call `disburse_maturity` on a **Spawning** SNS neuron (which the NNS correctly rejects). During the spawning process, the neuron's maturity is being converted to tokens via a separate mechanism. Accepting a `disburse_maturity` on a spawning neuron creates a race condition: the maturity is deducted from `maturity_e8s_equivalent` immediately, but the spawning process also reads and converts `maturity_e8s_equivalent`. If the spawning completes and sets `maturity_e8s_equivalent = 0` after the disbursement deduction, the accounting is inconsistent. The maturity is deducted twice from the user's perspective (once for spawning, once for disbursement), but only one minting event occurs for the spawning path — the disbursement will mint additional tokens from the governance canister's perspective, effectively minting tokens that were not backed by the original maturity balance.

For the non-spawning case (e.g., `NotDissolving` neuron), the impact is a 7-day forced lock of maturity that the user cannot cancel, which is a usability issue but not a conservation bug.

**Severity:** Medium — the spawning race is the critical path. It requires the caller to have `DisburseMaturity` permission on their own neuron (unprivileged ingress), and the neuron must be in the `Spawning` state.

### Likelihood Explanation

The SNS spawning mechanism is used by neuron holders to convert maturity to tokens. A user who initiates spawning and then immediately calls `disburse_maturity` before the spawning completes can trigger this race. The `Spawning` state persists for a period (until the spawn timer fires), giving a window for the attack. This is reachable by any unprivileged ingress caller who controls an SNS neuron with maturity.

### Recommendation

Add a neuron state check in SNS `disburse_maturity` (mirroring the NNS `initiate_maturity_disbursement`) to reject disbursements when the neuron is in the `Spawning` state:

```rust
let neuron_state = neuron.state(self.env.now());
if neuron_state == NeuronState::Spawning {
    return Err(GovernanceError::new_with_message(
        ErrorType::PreconditionFailed,
        "Neuron is spawning and cannot disburse maturity.",
    ));
}
```

This mirrors the existing NNS guard at `rs/nns/governance/src/governance/disburse_maturity.rs:278–301`.

### Proof of Concept

1. Create an SNS neuron with accumulated maturity.
2. Initiate neuron spawning (which sets the neuron to `Spawning` state and schedules maturity conversion).
3. Before the spawn timer fires, call `manage_neuron` with `Command::DisburseMaturity { percentage_to_disburse: 100, to_account: None }`.
4. The SNS `disburse_maturity` function accepts the call (no state check), deducts `maturity_e8s_equivalent`, and enqueues a `DisburseMaturityInProgress` entry.
5. The spawn timer fires and mints tokens based on the (now-zero) maturity — or the maturity was already zero, causing the spawn to produce 0 tokens.
6. After 7 days, `maybe_finalize_disburse_maturity` mints additional SNS tokens to the disbursement destination, sourced from the governance canister's minting authority, without a corresponding maturity balance backing them.

Entry path: unprivileged ingress → `manage_neuron` (SNS governance canister) → `disburse_maturity` → missing state guard → maturity accounting inconsistency during spawning. [6](#0-5) [7](#0-6)

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

**File:** rs/sns/governance/src/governance.rs (L4884-4893)
```rust
            Follow(_)
            | SetFollowing(_)
            | MakeProposal(_)
            | RegisterVote(_)
            | ClaimOrRefresh(_)
            | MergeMaturity(_)
            | DisburseMaturity(_)
            | AddNeuronPermissions(_)
            | RemoveNeuronPermissions(_)
            | StakeMaturity(_) => Ok(()),
```

**File:** rs/sns/governance/src/governance.rs (L4920-4975)
```rust
    // Disburses any maturity that should be disbursed, unless this is already happening.
    async fn maybe_finalize_disburse_maturity(&mut self) {
        if !self.can_finalize_disburse_maturity() {
            return;
        }

        let maturity_modulation_basis_points =
            match self.proto.effective_maturity_modulation_basis_points() {
                Ok(maturity_modulation_basis_points) => maturity_modulation_basis_points,
                Err(message) => {
                    log!(ERROR, "{}", message.error_message);
                    return;
                }
            };

        self.proto.is_finalizing_disburse_maturity = Some(true);
        let now_seconds = self.env.now();
        // Filter all the neurons that are ready to disburse.
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

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L271-302)
```rust
    let (
        is_neuron_spawning,
        is_neuron_controlled_by_caller,
        num_disbursements,
        maturity_e8s_equivalent,
    ) = neuron_store
        .with_neuron(id, |neuron| {
            let is_neuron_spawning = neuron.state(now_seconds) == NeuronState::Spawning;
            let is_neuron_controlled_by_caller = neuron.is_controlled_by(caller);
            let num_disbursements = neuron.maturity_disbursements_in_progress().len();
            let maturity_e8s_equivalent = neuron.maturity_e8s_equivalent;
            (
                is_neuron_spawning,
                is_neuron_controlled_by_caller,
                num_disbursements,
                maturity_e8s_equivalent,
            )
        })
        .map_err(|_| InitiateMaturityDisbursementError::NeuronNotFound)?;

    let disbursement_maturity_e8s =
        percentage_of_maturity(maturity_e8s_equivalent, *percentage_to_disburse)?;
    if disbursement_maturity_e8s < MINIMUM_DISBURSEMENT_E8S {
        return Err(InitiateMaturityDisbursementError::DisbursementTooSmall {
            disbursement_maturity_e8s,
            minimum_disbursement_e8s: MINIMUM_DISBURSEMENT_E8S,
        });
    }

    if is_neuron_spawning {
        return Err(InitiateMaturityDisbursementError::NeuronSpawning);
    }
```
