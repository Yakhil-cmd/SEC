### Title
Missing Lower-Bound Validation on `transfer_fee` in `ManageLedgerParameters` Proposal Allows Setting SNS Ledger Fee to Zero, Breaking `neuron_minimum_stake_e8s` Invariant - (File: `rs/sns/governance/src/proposal.rs`)

---

### Summary

The SNS governance canister's `validate_and_render_manage_ledger_parameters` function accepts any `u64` value for `transfer_fee`, including zero, without checking that the new fee is consistent with the existing `neuron_minimum_stake_e8s` parameter. A governance-passing `ManageLedgerParameters` proposal with `transfer_fee = Some(u64::MAX)` (or any value ≥ `neuron_minimum_stake_e8s`) can be submitted and executed, silently breaking the invariant `neuron_minimum_stake_e8s > transaction_fee_e8s` that the SNS system relies on for correct neuron staking, splitting, and disbursement.

---

### Finding Description

`validate_and_render_manage_ledger_parameters` in `rs/sns/governance/src/proposal.rs` (lines 1761–1799) validates a `ManageLedgerParameters` proposal. When `transfer_fee` is `Some(value)`, the function only records the change for rendering — it performs **no bounds check** on the fee value:

```rust
if let Some(transfer_fee) = transfer_fee {
    render += &format!("# Set token transfer fee: {transfer_fee} token-quantums. \n",);
    change = true;
}
```

No check is made that the new `transfer_fee` is less than the current `neuron_minimum_stake_e8s` stored in `NervousSystemParameters`.

After proposal execution, `perform_manage_ledger_parameters` in `rs/sns/governance/src/governance.rs` (lines 3190–3195) writes the new fee directly into `nervous_system_parameters.transaction_fee_e8s`:

```rust
if let Some(nervous_system_parameters) = self.proto.parameters.as_mut()
    && let Some(transfer_fee) = manage_ledger_parameters.transfer_fee
{
    nervous_system_parameters.transaction_fee_e8s = Some(transfer_fee);
}
```

This update is performed **without re-validating** the `NervousSystemParameters` invariant that `neuron_minimum_stake_e8s > transaction_fee_e8s`, which is enforced at init time and via `ManageNervousSystemParameters` proposals (see `validate_neuron_minimum_stake_e8s` in `rs/sns/governance/src/types.rs`, lines 602–618).

The `ManageLedgerParameters` proposal path is a **separate, unguarded code path** that bypasses this cross-parameter invariant check entirely.

---

### Impact Explanation

If a `ManageLedgerParameters` proposal sets `transfer_fee` to a value ≥ `neuron_minimum_stake_e8s` (e.g., `u64::MAX` or any value exceeding the current minimum stake), the following invariant is silently broken:

> `neuron_minimum_stake_e8s > transaction_fee_e8s`

This invariant is relied upon in multiple places:
- **Neuron splitting** (`rs/sns/governance/src/governance.rs`, line 1318): `split.amount_e8s < min_stake + transaction_fee_e8s` — with a huge fee, no split is ever possible.
- **Neuron staking validation** (`rs/sns/governance/src/types.rs`, line 610): `neuron_minimum_stake_e8s <= transaction_fee_e8s` would now be true, meaning any future `ManageNervousSystemParameters` proposal that touches these fields would be rejected as invalid, potentially locking the SNS into a broken state.
- **Swap finalization** (`rs/sns/init/src/lib.rs`, line 1628): `neuron_minimum_stake_e8s <= sns_transaction_fee_e8s` causes swap validation to fail.

Setting `transfer_fee = 0` (also unchecked) removes all fee collection from the SNS ledger, breaking fee-based spam protection and fee-collector accounting.

---

### Likelihood Explanation

The `ManageLedgerParameters` action (Action ID 13) is a standard SNS governance proposal type, submittable by any neuron holder with sufficient stake. The proposal passes through normal SNS voting. The only validation gate is `validate_and_render_manage_ledger_parameters`, which does not check the fee value. Any SNS community member who can pass a governance vote (or a malicious actor who accumulates sufficient voting power) can trigger this. The attack path is fully on-chain and requires no privileged access beyond normal SNS neuron ownership.

---

### Recommendation

In `validate_and_render_manage_ledger_parameters` (`rs/sns/governance/src/proposal.rs`), add a cross-parameter check when `transfer_fee` is set:

```rust
if let Some(transfer_fee) = transfer_fee {
    // Retrieve current neuron_minimum_stake_e8s from governance parameters
    // and enforce: transfer_fee < neuron_minimum_stake_e8s
    if let Some(min_stake) = current_parameters.neuron_minimum_stake_e8s {
        if *transfer_fee >= min_stake {
            return Err(format!(
                "transfer_fee ({transfer_fee}) must be less than neuron_minimum_stake_e8s ({min_stake})"
            ));
        }
    }
    render += &format!("# Set token transfer fee: {transfer_fee} token-quantums. \n");
    change = true;
}
```

This requires passing `current_parameters: &NervousSystemParameters` into the function (analogous to how `validate_and_render_manage_nervous_system_parameters` already receives `current_parameters`). Additionally, after execution in `perform_manage_ledger_parameters`, re-validate the full `NervousSystemParameters` to catch any invariant violations before committing.

---

### Proof of Concept

1. An SNS is deployed with `neuron_minimum_stake_e8s = 400_000_000` and `transaction_fee_e8s = 10_000`.
2. A neuron holder submits a `ManageLedgerParameters` proposal with `transfer_fee = Some(500_000_000)` (greater than `neuron_minimum_stake_e8s`).
3. `validate_and_render_manage_ledger_parameters` accepts it — no fee bounds check exists.
4. The proposal passes governance voting and executes via `perform_manage_ledger_parameters`.
5. `nervous_system_parameters.transaction_fee_e8s` is set to `500_000_000`.
6. Now `transaction_fee_e8s (500_000_000) > neuron_minimum_stake_e8s (400_000_000)`.
7. Any subsequent `ManageNervousSystemParameters` proposal touching `neuron_minimum_stake_e8s` or `transaction_fee_e8s` will fail `validate_neuron_minimum_stake_e8s` (line 610), making the SNS governance parameters permanently unmodifiable via that path.
8. Neuron splitting is broken: `split.amount_e8s < min_stake + transaction_fee_e8s` = `400_000_000 + 500_000_000 = 900_000_000`, requiring 9 ICP minimum to split any neuron.

**Root cause file:** `rs/sns/governance/src/proposal.rs`, lines 1773–1776 [1](#0-0) 

**Execution path that writes the unchecked value:** `rs/sns/governance/src/governance.rs`, lines 3191–3195 [2](#0-1) 

**Invariant that is broken:** `rs/sns/governance/src/types.rs`, lines 610–614 [3](#0-2) 

**Neuron split check that becomes permanently broken:** `rs/sns/governance/src/governance.rs`, line 1318 [4](#0-3)

### Citations

**File:** rs/sns/governance/src/proposal.rs (L1773-1776)
```rust
    if let Some(transfer_fee) = transfer_fee {
        render += &format!("# Set token transfer fee: {transfer_fee} token-quantums. \n",);
        change = true;
    }
```

**File:** rs/sns/governance/src/governance.rs (L1191-3195)
```rust
                .await?;

            // We only update the cached_neuron_stake_e8s and neuron_fees_e8s if we actually
            // burn fees, otherwise this leads to ledger and governance getting out of sync.
            let nid = id.to_string();
            let neuron = self
                .proto
                .neurons
                .get_mut(&nid)
                .expect("Expected the parent neuron to exist");

            // Update the neuron's stake and management fees to reflect the burning
            // above.
            neuron.cached_neuron_stake_e8s = neuron
                .cached_neuron_stake_e8s
                .saturating_sub(max_burnable_fee);

            neuron.neuron_fees_e8s = neuron.neuron_fees_e8s.saturating_sub(max_burnable_fee);
        }

        // Transfer 2 - Disburse to the chosen account. This may fail if the
        // user told us to disburse more than they had in their account (but
        // the burn still happened).
        let block_height = self
            .ledger
            .transfer_funds(
                disburse_amount_e8s,
                transaction_fee_e8s,
                Some(from_subaccount),
                to_account,
                self.env.now(),
            )
            .await?;

        let nid = id.to_string();
        let neuron = self
            .proto
            .neurons
            .get_mut(&nid)
            .expect("Expected the parent neuron to exist");

        let to_deduct = disburse_amount_e8s + transaction_fee_e8s;
        // The transfer was successful we can change the stake of the neuron.
        neuron.cached_neuron_stake_e8s = neuron.cached_neuron_stake_e8s.saturating_sub(to_deduct);

        Ok(block_height)
    }

    /// Returns the maximum amount of fees that can be burned for a given neuron.
    /// This takes into account the open proposals that this neuron has submitted,
    /// ensuring we don't burn fees that could potentially be refunded if those
    /// proposals are accepted.
    fn maximum_burnable_fees_for_neuron(&self, neuron: &Neuron) -> Result<u64, GovernanceError> {
        let neuron_id = neuron.id.as_ref().ok_or_else(|| {
            GovernanceError::new_with_message(ErrorType::NotFound, "Neuron does not have an ID")
        })?;

        // Calculate the total reject costs from all open proposals submitted by this neuron
        let total_open_proposal_reject_costs = self
            .proto
            .proposals
            .values()
            .filter(|proposal_data| {
                // Only consider open proposals where this neuron is the proposer
                proposal_data.proposer.as_ref() == Some(neuron_id)
                    && proposal_data.status() == ProposalDecisionStatus::Open
            })
            .map(|proposal_data| proposal_data.reject_cost_e8s)
            .sum::<u64>();

        // The maximum burnable amount is the total fees minus any fees that are
        // tied up in open proposals (which could potentially be refunded)
        let max_burnable = neuron
            .neuron_fees_e8s
            .saturating_sub(total_open_proposal_reject_costs);

        Ok(max_burnable)
    }

    /// Splits a (parent) neuron into two neurons (the parent and child neuron).
    ///
    /// The parent neuron's cached stake is decreased by the amount specified in
    /// Split, while the child neuron is created with a stake equal to that
    /// amount, minus the transfer fee.
    /// The management fees and the maturity remain in the parent neuron.
    ///
    /// The child neuron inherits all the properties of its parent
    /// including age and dissolve state.
    ///
    /// On success returns the newly created neuron's id.
    ///
    /// Preconditions:
    /// - The heap can grow
    /// - The parent neuron exists
    /// - The caller is authorized to perform this neuron operation
    ///   (NeuronPermissionType::Split)
    /// - The amount to split minus the transfer fee is more than the minimum
    ///   stake (thus the child neuron will have at least the minimum stake)
    /// - The parent's stake minus amount to split is more than the minimum
    ///   stake (thus the parent neuron will have at least the minimum stake)
    /// - The parent neuron's id is not in the list of neurons with ongoing operations
    pub async fn split_neuron(
        &mut self,
        id: &NeuronId,
        caller: &PrincipalId,
        split: &manage_neuron::Split,
    ) -> Result<NeuronId, GovernanceError> {
        // New neurons are not allowed when the heap is too large.
        self.check_heap_can_grow()?;

        let min_stake = self
            .proto
            .parameters
            .as_ref()
            .expect("Governance must have NervousSystemParameters.")
            .neuron_minimum_stake_e8s
            .expect("NervousSystemParameters must have neuron_minimum_stake_e8s");

        let transaction_fee_e8s = self.transaction_fee_e8s_or_panic();

        // Get the neuron and clone to appease the borrow checker.
        // We'll get a mutable reference when we need to change it later.
        let parent_neuron = self.get_neuron_result(id)?.clone();
        let parent_nid = parent_neuron.id.as_ref().expect("Neurons must have an id");

        parent_neuron.check_authorized(caller, NeuronPermissionType::Split)?;

        if split.amount_e8s < min_stake + transaction_fee_e8s {
            return Err(GovernanceError::new_with_message(
                ErrorType::InsufficientFunds,
                format!(
                    "Trying to split a neuron with argument {} e8s. This is too little: \
                      at the minimum, one needs the minimum neuron stake, which is {} e8s, \
                      plus the transaction fee, which is {}. Hence the minimum split amount is {}.",
                    split.amount_e8s,
                    min_stake,
                    transaction_fee_e8s,
                    min_stake + transaction_fee_e8s
                ),
            ));
        }

        if parent_neuron.stake_e8s() < min_stake + split.amount_e8s {
            return Err(GovernanceError::new_with_message(
                ErrorType::InsufficientFunds,
                format!(
                    "Trying to split {} e8s out of neuron {}. \
                     This is not allowed, because the parent has stake {} e8s. \
                     If the requested amount was subtracted from it, there would be less than \
                     the minimum allowed stake, which is {} e8s. ",
                    split.amount_e8s,
                    parent_nid,
                    parent_neuron.stake_e8s(),
                    min_stake
                ),
            ));
        }

        let creation_timestamp_seconds = self.env.now();

        let from_subaccount = parent_neuron.subaccount()?;

        let child_nid = self.new_neuron_id(caller, split.memo)?;
        let to_subaccount = child_nid.subaccount()?;

        let staked_amount = split.amount_e8s - transaction_fee_e8s;

        // Before we do the transfer, we need to save the child neuron in the map
        // otherwise a trap after the transfer is successful but before this
        // method finishes would cause the funds to be lost.
        // However the new neuron is not yet ready to be used as we can't know
        // whether the transfer will succeed, so we temporarily set the
        // stake to 0 and only change it after the transfer is successful.
        let child_neuron = Neuron {
            id: Some(child_nid.clone()),
            permissions: parent_neuron.permissions.clone(),
            cached_neuron_stake_e8s: 0,
            neuron_fees_e8s: 0,
            created_timestamp_seconds: creation_timestamp_seconds,
            aging_since_timestamp_seconds: parent_neuron.aging_since_timestamp_seconds,
            followees: parent_neuron.followees.clone(),
            topic_followees: parent_neuron.topic_followees.clone(),
            maturity_e8s_equivalent: 0,
            dissolve_state: parent_neuron.dissolve_state,
            voting_power_percentage_multiplier: parent_neuron.voting_power_percentage_multiplier,
            source_nns_neuron_id: parent_neuron.source_nns_neuron_id,
            staked_maturity_e8s_equivalent: None,
            auto_stake_maturity: parent_neuron.auto_stake_maturity,
            vesting_period_seconds: None,
            disburse_maturity_in_progress: vec![],
        };

        // Add the child neuron's id to the set of neurons with ongoing operations.
        let in_flight_command = NeuronInFlightCommand {
            timestamp: creation_timestamp_seconds,
            command: Some(InFlightCommand::Split(*split)),
        };
        let _child_lock = self.lock_neuron_for_command(&child_nid, in_flight_command)?;

        // We need to add the "embryo neuron" to the governance proto only after
        // acquiring the lock. Indeed, in case there is already a pending
        // command, we return without state rollback. If we had already created
        // the embryo, it would not be garbage collected.
        self.add_neuron(child_neuron.clone())?;

        // Do the transfer.
        let result: Result<u64, NervousSystemError> = self
            .ledger
            .transfer_funds(
                staked_amount,
                transaction_fee_e8s,
                Some(from_subaccount),
                self.neuron_account_id(to_subaccount),
                split.memo,
            )
            .await;

        if let Err(error) = result {
            let error = GovernanceError::from(error);
            // If we've got an error, we assume the transfer didn't happen for
            // some reason. The only state to cleanup is to delete the child
            // neuron, since we haven't mutated the parent yet.
            self.remove_neuron(&child_nid, child_neuron)?;
            log!(
                ERROR,
                "Neuron stake transfer of split_neuron: {:?} \
                     failed with error: {:?}. Neuron can't be staked.",
                child_nid,
                error
            );
            return Err(error);
        }

        // Get the neuron again, but this time a mutable reference.
        // Expect it to exist, since we acquired a lock above.
        let parent_neuron = self.get_neuron_result_mut(id).expect("Neuron not found");

        // Update the state of the parent and child neuron.
        parent_neuron.cached_neuron_stake_e8s -= split.amount_e8s;

        let child_neuron = self
            .get_neuron_result_mut(&child_nid)
            .expect("Expected the child neuron to exist");

        child_neuron.cached_neuron_stake_e8s = staked_amount;
        Ok(child_nid)
    }

    /// Merges the maturity of a neuron into the neuron's cached stake.
    ///
    /// This method allows a neuron controller to merge the currently
    /// existing maturity of a neuron into the neuron's stake. The
    /// caller can choose a percentage of maturity to merge.
    ///
    /// Pre-conditions:
    /// - The neuron exists
    /// - The caller is authorized to perform this neuron operation
    ///   (NeuronPermissionType::MergeMaturity)
    /// - The given percentage_to_merge is between 1 and 100 (inclusive)
    /// - The e8s equivalent of the amount of maturity to merge is more
    ///   than the transaction fee.
    /// - The neuron's id is not yet in the list of neurons with ongoing operations
    pub async fn merge_maturity(
        &mut self,
        id: &NeuronId,
        caller: &PrincipalId,
        merge_maturity: &manage_neuron::MergeMaturity,
    ) -> Result<MergeMaturityResponse, GovernanceError> {
        let now = self.env.now();

        let neuron = self.get_neuron_result(id)?.clone();

        neuron.check_authorized(caller, NeuronPermissionType::MergeMaturity)?;

        if merge_maturity.percentage_to_merge > 100 || merge_maturity.percentage_to_merge == 0 {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "The percentage of maturity to merge must be a value between 1 and 100 (inclusive).",
            ));
        }

        let transaction_fee_e8s = self.transaction_fee_e8s_or_panic();

        let mut maturity_to_merge =
            (neuron.maturity_e8s_equivalent * merge_maturity.percentage_to_merge as u64) / 100;

        // Converting u64 to f64 can cause the u64 to be "rounded up", so we
        // need to account for this possibility.
        if maturity_to_merge > neuron.maturity_e8s_equivalent {
            maturity_to_merge = neuron.maturity_e8s_equivalent;
        }

        if maturity_to_merge <= transaction_fee_e8s {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!(
                    "Tried to merge {maturity_to_merge} e8s, but can't merge an amount less than the transaction fee of {transaction_fee_e8s} e8s"
                ),
            ));
        }

        let nid = neuron.id.as_ref().expect("Neurons must have an id");
        let subaccount = neuron.subaccount()?;

        // Do the transfer, this is a minting transfer, from the governance canister's
        // (which is also the minting canister) main account into the neuron's
        // subaccount.
        #[rustfmt::skip]
        let _block_height: u64 = self
            .ledger
            .transfer_funds(
                maturity_to_merge,
                0, // Minting transfer don't pay a fee
                None, // This is a minting transfer, no 'from' account is needed
                self.neuron_account_id(subaccount), // The account of the neuron on the ledger
                self.env.insecure_random_u64(), // Random memo(nonce) for the ledger's transaction
            )
            .await?;

        // Adjust the maturity, stake, and age of the neuron
        let neuron = self
            .get_neuron_result_mut(nid)
            .expect("Expected the neuron to exist");

        neuron.maturity_e8s_equivalent = neuron
            .maturity_e8s_equivalent
            .saturating_sub(maturity_to_merge);
        let new_stake = neuron
            .cached_neuron_stake_e8s
            .saturating_add(maturity_to_merge);
        neuron.update_stake(new_stake, now);
        let new_stake_e8s = neuron.cached_neuron_stake_e8s;

        Ok(MergeMaturityResponse {
            merged_maturity_e8s: maturity_to_merge,
            new_stake_e8s,
        })
    }

    /// Stakes the maturity of a neuron.
    ///
    /// This method allows a neuron controller to stake the currently
    /// existing maturity of a neuron. The caller can choose a percentage
    /// of maturity to merge.
    ///
    /// Pre-conditions:
    /// - The neuron is locked for exclusive use (ALL manage_neuron operation lock the neuron)
    /// - The neuron is controlled by `caller`
    /// - The neuron has some maturity to stake.
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

    /// Disburses a neuron's maturity.
    ///
    /// This causes the neuron's maturity to be disbursed to the provided
    /// ledger account. If no ledger account is given, the caller's default
    /// account is used.
    /// The caller can choose a percentage of maturity to disburse.
    ///
    /// Pre-conditions:
    /// - The neuron exists
    /// - The caller is authorized to perform this neuron operation
    ///   (NeuronPermissionType::DisburseMaturity)
    /// - The given percentage_to_merge is between 1 and 100 (inclusive)
    /// - The neuron's id is not yet in the list of neurons with ongoing operations
    /// - The e8s equivalent of the amount of maturity to disburse is more
    ///   than the transaction fee.
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

    /// Sets a proposal's status to 'executed' or 'failed' depending on the given result that
    /// was returned by the method that was supposed to execute the proposal.
    ///
    /// The proposal ID 'pid' is taken as a raw integer to avoid
    /// lifetime issues.
    ///
    /// Pre-conditions:
    /// - The proposal's decision status is ProposalStatusAdopted
    pub fn set_proposal_execution_status(&mut self, pid: u64, result: Result<(), GovernanceError>) {
        match self.proto.proposals.get_mut(&pid) {
            Some(proposal) => {
                // The proposal has to be adopted before it is executed.
                assert_eq!(proposal.status(), ProposalDecisionStatus::Adopted);
                match result {
                    Ok(_) => {
                        log!(INFO, "Execution of proposal: {} succeeded.", pid);
                        // The proposal was executed 'now'.
                        proposal.executed_timestamp_seconds = self.env.now();
                        // If the proposal was executed it has not failed,
                        // thus we set the failed_timestamp_seconds to zero
                        // (it should already be zero, but let's be defensive).
                        proposal.failed_timestamp_seconds = 0;
                        proposal.failure_reason = None;
                    }
                    Err(error) => {
                        log!(
                            ERROR,
                            "Execution of proposal: {} failed. Reason: {:?}",
                            pid,
                            error
                        );
                        // To ensure that we don't update the failure timestamp
                        // if there has been success, check if executed_timestamp_seconds
                        // is set to a non-zero value (this should not happen).
                        // Then, record that the proposal failed 'now' with the
                        // given error.
                        if proposal.executed_timestamp_seconds == 0 {
                            proposal.failed_timestamp_seconds = self.env.now();
                            proposal.failure_reason = Some(error);
                        }
                    }
                }
            }
            None => {
                // The proposal ID was not found. Something is wrong:
                // just log this information to aid debugging.
                log!(
                    ERROR,
                    "Proposal {:?} not found when attempt to set execution result to {:?}",
                    pid,
                    result
                );
            }
        }
    }

    /// Returns the latest reward event.
    pub fn latest_reward_event(&self) -> RewardEvent {
        self.proto
            .latest_reward_event
            .as_ref()
            .expect("Invariant violation! There should always be a latest_reward_event.")
            .clone()
    }

    /// Tries to get a proposal given a proposal id.
    pub fn get_proposal(&self, req: &GetProposal) -> GetProposalResponse {
        let pid = req.proposal_id.expect("GetProposal must have proposal_id");
        let proposal_data = match self.proto.proposals.get(&pid.id) {
            None => get_proposal_response::Result::Error(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "No proposal for given ProposalId.",
            )),
            Some(pd) => get_proposal_response::Result::Proposal(pd.limited_for_get_proposal()),
        };

        GetProposalResponse {
            result: Some(proposal_data),
        }
    }

    /// Returns proposal data of proposals with proposal ID less
    /// than `before_proposal` (exclusive), returning at most `limit` proposal
    /// data. If `before_proposal` is not provided, list_proposals() starts from the highest
    /// available proposal ID (inclusive). If `limit` is not provided, the
    /// system max MAX_LIST_PROPOSAL_RESULTS is used.
    ///
    /// As proposal IDs are assigned sequentially, this retrieves up to
    /// `limit` proposals older (in terms of creation) than a specific
    /// proposal. This can be used to paginate through proposals, as follows:
    ///
    /// `
    /// let mut lst = gov.list_proposals(ListProposalInfo {});
    /// while !lst.empty() {
    ///   /* do stuff with lst */
    ///   lst = gov.list_proposals(ListProposalInfo {
    ///     before_proposal: lst.last().and_then(|x|x.id)
    ///   });
    /// }
    /// `
    ///
    /// The proposals' ballots are not returned in the `ListProposalResponse`.
    /// Proposals with `ExecuteNervousSystemFunction` as action have their
    /// `payload` cleared if larger than
    /// EXECUTE_NERVOUS_SYSTEM_FUNCTION_PAYLOAD_LISTING_BYTES_MAX.
    ///
    /// The caller can retrieve dropped payloads and ballots by calling `get_proposal`
    /// for each proposal of interest.
    pub fn list_proposals(
        &self,
        request: &ListProposals,
        caller: &PrincipalId,
    ) -> ListProposalsResponse {
        let caller_neurons_set: HashSet<_> = self
            .get_neuron_ids_by_principal(caller)
            .into_iter()
            .map(|neuron_id| neuron_id.to_string())
            .collect();
        let exclude_type: HashSet<u64> = request.exclude_type.iter().cloned().collect();
        let include_reward_status: HashSet<i32> =
            request.include_reward_status.iter().cloned().collect();
        let include_status: HashSet<i32> = request.include_status.iter().cloned().collect();
        let include_topics: HashSet<Option<Topic>> = request
            .include_topics
            .iter()
            .map(|topic_selector| {
                topic_selector
                    .topic
                    .and_then(|topic| Topic::try_from(topic).ok())
            })
            .collect();
        let now = self.env.now();
        let filter_all = |data: &ProposalData| -> bool {
            let action = data.action;
            // Filter out proposals by action.
            if exclude_type.contains(&action) {
                return false;
            }
            // Filter out proposals by reward status.
            if !(include_reward_status.is_empty()
                || include_reward_status.contains(&(data.reward_status(now) as i32)))
            {
                return false;
            }
            // Filter out proposals by decision status.
            if !(include_status.is_empty() || include_status.contains(&(data.status() as i32))) {
                return false;
            }
            // Filter out proposals by topic.
            let topic = data.topic.and_then(|topic| Topic::try_from(topic).ok());
            if !(include_topics.is_empty() || include_topics.contains(&topic)) {
                return false;
            }

            true
        };
        let limit = if request.limit == 0 || request.limit > MAX_LIST_PROPOSAL_RESULTS {
            MAX_LIST_PROPOSAL_RESULTS
        } else {
            request.limit
        } as usize;
        let props = &self.proto.proposals;
        // Proposals are stored in a sorted map. If 'before_proposal'
        // is provided, grab all proposals before that, else grab the
        // whole range.
        let rng = if let Some(n) = request.before_proposal {
            props.range(..(n.id))
        } else {
            props.range(..)
        };
        // Now reverse the range, filter, and restrict to 'limit'.
        let limited_rng = rng
            .rev()
            .filter(|(_, proposal)| filter_all(proposal))
            .take(limit);

        let proposal_info = limited_rng
            .map(|(_id, proposal_data)| {
                proposal_data.limited_for_list_proposals(&caller_neurons_set)
            })
            .collect();

        // Ignore the keys and clone to a vector.
        ListProposalsResponse {
            proposals: proposal_info,
            include_ballots_by_caller: Some(true),
            include_topic_filtering: Some(true),
        }
    }

    /// Returns a list of all existing nervous system functions
    pub fn list_nervous_system_functions(&self) -> ListNervousSystemFunctionsResponse {
        let functions = Action::native_functions()
            .into_iter()
            .chain(
                self.proto
                    .id_to_nervous_system_functions
                    .values()
                    .filter(|&f| f != &*NERVOUS_SYSTEM_FUNCTION_DELETION_MARKER)
                    .cloned(),
            )
            .collect();

        // Get the set of ids that have been used in the past.
        let reserved_ids = self
            .proto
            .id_to_nervous_system_functions
            .iter()
            .filter(|(_, f)| f == &&*NERVOUS_SYSTEM_FUNCTION_DELETION_MARKER)
            .map(|(id, _)| *id)
            .collect();

        ListNervousSystemFunctionsResponse {
            functions,
            reserved_ids,
        }
    }

    /// Returns the proposal IDs for all proposals that have reward status ReadyToSettle
    fn ready_to_be_settled_proposal_ids(&self) -> impl Iterator<Item = ProposalId> + '_ {
        let now = self.env.now();
        self.proto
            .proposals
            .iter()
            .filter(move |(_, data)| data.reward_status(now) == ProposalRewardStatus::ReadyToSettle)
            .map(|(k, _)| ProposalId { id: *k })
    }

    /// Attempts to move the proposal with the given ID forward in the process,
    /// from open to adopted or rejected and from adopted to executed or failed.
    ///
    /// If the proposal is open, tallies the ballots and updates the `yes`, `no`, and
    /// `undecided` voting power accordingly.
    /// This may result in the proposal becoming adopted or rejected.
    ///
    /// If the proposal is adopted but not executed, attempts to execute it.
    pub fn process_proposal(&mut self, proposal_id: u64) {
        let now_seconds = self.env.now();

        let proposal_data = match self.proto.proposals.get_mut(&proposal_id) {
            None => return,
            Some(p) => p,
        };

        // Recompute the tally here. It should correctly reflect all votes until
        // the deadline, even after the proposal has been decided.
        if proposal_data.status() == ProposalDecisionStatus::Open
            || proposal_data.accepts_vote(now_seconds)
        {
            proposal_data.recompute_tally(now_seconds);
        }

        // If the status is open
        if proposal_data.status() != ProposalDecisionStatus::Open
            || !proposal_data.can_make_decision(now_seconds)
        {
            return;
        }

        // This marks the proposal_data as no longer open.
        proposal_data.decided_timestamp_seconds = now_seconds;
        if !proposal_data.is_accepted() {
            return;
        }

        // Return the rejection fee to the proposal's proposer
        if let Some(nid) = &proposal_data.proposer
            && let Some(neuron) = self.proto.neurons.get_mut(&nid.to_string())
            && neuron.neuron_fees_e8s >= proposal_data.reject_cost_e8s
        {
            neuron.neuron_fees_e8s -= proposal_data.reject_cost_e8s;
        }

        // A yes decision has been made, execute the proposal!
        // Safely unwrap action.
        let action = proposal_data
            .proposal
            .as_ref()
            .and_then(|p| p.action.clone());
        let action = match action {
            Some(action) => action,

            // This should not be possible, because proposal validation should
            // have been performed when the proposal was first made.
            None => {
                self.set_proposal_execution_status(
                    proposal_id,
                    Err(GovernanceError::new_with_message(
                        ErrorType::InvalidProposal,
                        "Proposal has no action.",
                    )),
                );
                return;
            }
        };
        self.start_proposal_execution(proposal_id, action);
    }

    /// Processes all proposals with decision status ProposalStatusOpen
    pub fn process_proposals(&mut self) {
        if self.env.now() < self.closest_proposal_deadline_timestamp_seconds {
            // Nothing to do.
            return;
        }

        let pids = self
            .proto
            .proposals
            .iter()
            .filter(|(_, info)| {
                info.status() == ProposalDecisionStatus::Open || info.accepts_vote(self.env.now())
            })
            .map(|(pid, _)| *pid)
            .collect::<Vec<u64>>();

        for pid in pids {
            self.process_proposal(pid);
        }

        self.closest_proposal_deadline_timestamp_seconds = self
            .proto
            .proposals
            .values()
            .filter(|data| data.status() == ProposalDecisionStatus::Open)
            .map(|proposal_data| {
                proposal_data
                    .wait_for_quiet_state
                    .map(|w| w.current_deadline_timestamp_seconds)
                    .unwrap_or_else(|| {
                        proposal_data
                            .proposal_creation_timestamp_seconds
                            .saturating_add(proposal_data.initial_voting_period_seconds)
                    })
            })
            .min()
            .unwrap_or(u64::MAX);
    }

    pub async fn get_metrics(&self, time_window_seconds: u64) -> Result<Metrics, GovernanceError> {
        let num_recently_submitted_proposals =
            self.recently_submitted_proposals(time_window_seconds);

        let num_recently_executed_proposals = self.recently_executed_proposals(time_window_seconds);

        let icrc_ledger_helper = ICRCLedgerHelper::with_ledger(self.ledger.as_ref());

        let last_ledger_block_timestamp = icrc_ledger_helper
            .get_latest_block_timestamp_seconds()
            .await
            .map_err(|error_mesage| {
                GovernanceError::new_with_message(ErrorType::External, error_mesage)
            })?;

        let treasury_metrics = self
            .proto
            .metrics
            .as_ref()
            .map(|metrics| metrics.treasury_metrics.clone())
            .unwrap_or_default();

        let voting_power_metrics = self
            .proto
            .metrics
            .as_ref()
            .map(|metrics| metrics.voting_power_metrics)
            .unwrap_or_default();

        let genesis_timestamp_seconds = self.proto.genesis_timestamp_seconds;

        Ok(Metrics {
            num_recently_submitted_proposals,
            num_recently_executed_proposals,
            last_ledger_block_timestamp,
            treasury_metrics,
            voting_power_metrics,
            genesis_timestamp_seconds,
        })
    }

    fn recently_submitted_proposals(&self, time_window_seconds: u64) -> u64 {
        self.proto
            .proposals
            .values()
            .rev()
            .take_while(|proposal| {
                self.env
                    .now()
                    .saturating_sub(proposal.proposal_creation_timestamp_seconds)
                    <= time_window_seconds
            })
            .count() as u64
    }

    fn recently_executed_proposals(&self, time_window_seconds: u64) -> u64 {
        self.proto
            .proposals
            .values()
            .filter(|proposal| {
                self.env
                    .now()
                    .saturating_sub(proposal.executed_timestamp_seconds)
                    <= time_window_seconds
            })
            .count() as u64
    }

    /// Starts execution of the given proposal in the background.
    ///
    /// The given proposal ID specifies the proposal and the `action` specifies
    /// what the proposal should do (basically, function and parameters to be applied).
    fn start_proposal_execution(&mut self, proposal_id: u64, action: Action) {
        // `perform_action` is an async method of &mut self.
        //
        // Starting it and letting it run in the background requires knowing that
        // the `self` reference will last until the future has completed.
        //
        // The compiler cannot know that, but this is actually true:
        //
        // - in unit tests, all futures are immediately ready, because no real async
        //   call is made. In this case, the transmutation to a static ref is abusive,
        //   but it's still ok since the future will immediately resolve.
        //
        // - in prod, "self" is a reference to the GOVERNANCE static variable, which is
        //   initialized only once (in canister_init or canister_post_upgrade)
        let governance: &'static mut Governance = unsafe { std::mem::transmute(self) };
        spawn_in_canister_env(governance.perform_action(proposal_id, action));
    }

    /// For a given proposal (given by its ID), selects and performs the right 'action',
    /// that is what this proposal is supposed to do as a result of the proposal being
    /// adopted.
    async fn perform_action(&mut self, proposal_id: u64, action: Action) {
        let result = match action {
            // Execution of Motion proposals is trivial.
            Action::Motion(_) => Ok(()),

            Action::ManageNervousSystemParameters(params) => {
                self.perform_manage_nervous_system_parameters(params)
            }
            Action::UpgradeSnsControlledCanister(params) => {
                self.perform_upgrade_sns_controlled_canister(proposal_id, params)
                    .await
            }
            Action::UpgradeSnsToNextVersion(_) => {
                log!(INFO, "Executing UpgradeSnsToNextVersion action",);
                let upgrade_sns_result = self
                    .perform_upgrade_to_next_sns_version_legacy(proposal_id)
                    .await;

                // If the upgrade returned `Ok(true)` that means the upgrade completed successfully
                // and the proposal can be marked as "executed". If the upgrade returned `Ok(false)`
                // that means the upgrade has successfully been kicked-off asynchronously, but not
                // completed. Governance's run_periodic_tasks logic will continuously check
                // the status of the upgrade and mark the proposal as either executed or failed.
                // So we call `return` in the `Ok(false)` branch so that
                // `set_proposal_execution_status` doesn't get called and set the proposal status
                // prematurely. If the result is `Err`, we do want to set the proposal status,
                // and passing the value through is sufficient.
                match upgrade_sns_result {
                    Ok(true) => Ok(()),
                    Ok(false) => return,
                    Err(e) => Err(e),
                }
            }
            Action::ExecuteGenericNervousSystemFunction(call) => {
                self.perform_execute_generic_nervous_system_function(call)
                    .await
            }
            Action::ExecuteExtensionOperation(execute_extension_operation) => {
                self.perform_execute_extension_operation(execute_extension_operation)
                    .await
            }
            Action::AddGenericNervousSystemFunction(nervous_system_function) => {
                self.perform_add_generic_nervous_system_function(nervous_system_function)
            }
            Action::RemoveGenericNervousSystemFunction(id) => {
                self.perform_remove_generic_nervous_system_function(id)
            }
            Action::RegisterDappCanisters(register_dapp_canisters) => {
                self.perform_register_dapp_canisters(register_dapp_canisters)
                    .await
            }
            Action::RegisterExtension(register_extension) => {
                self.perform_register_extension(register_extension).await
            }
            Action::UpgradeExtension(upgrade_extension) => {
                self.perform_upgrade_extension(upgrade_extension).await
            }
            Action::DeregisterDappCanisters(deregister_dapp_canisters) => {
                self.perform_deregister_dapp_canisters(deregister_dapp_canisters)
                    .await
            }
            Action::ManageSnsMetadata(manage_sns_metadata) => {
                self.perform_manage_sns_metadata(manage_sns_metadata)
            }
            Action::TransferSnsTreasuryFunds(transfer) => {
                let valuation =
                    get_action_auxiliary(&self.proto.proposals, ProposalId { id: proposal_id })
                        .and_then(|action_auxiliary| {
                            action_auxiliary.unwrap_transfer_sns_treasury_funds_or_err()
                        });
                self.perform_transfer_sns_treasury_funds(proposal_id, valuation, &transfer)
                    .await
            }
            Action::MintSnsTokens(mint) => self.perform_mint_sns_tokens(mint).await,
            Action::ManageLedgerParameters(manage_ledger_parameters) => {
                self.perform_manage_ledger_parameters(proposal_id, manage_ledger_parameters)
                    .await
            }
            Action::ManageDappCanisterSettings(manage_dapp_canister_settings) => {
                self.perform_manage_dapp_canister_settings(manage_dapp_canister_settings)
                    .await
            }
            Action::AdvanceSnsTargetVersion(_) => {
                get_action_auxiliary(&self.proto.proposals, ProposalId { id: proposal_id })
                    .and_then(|action_auxiliary| {
                        action_auxiliary.unwrap_advance_sns_target_version_or_err()
                    })
                    .and_then(|new_target| self.perform_advance_target_version(new_target))
            }
            Action::SetTopicsForCustomProposals(set_topics_for_custom_proposals) => {
                self.perform_set_topics_for_custom_proposals(set_topics_for_custom_proposals)
            }
            // This should not be possible, because Proposal validation is performed when
            // a proposal is first made.
            Action::Unspecified(_) => Err(GovernanceError::new_with_message(
                ErrorType::InvalidProposal,
                format!(
                    "A Proposal somehow made it all the way to execution despite being \
                         invalid for having its `unspecified` field populated. action: {action:?}"
                ),
            )),
        };

        self.set_proposal_execution_status(proposal_id, result);
    }

    /// Adds a new nervous system function to Governance if the given id for the nervous system
    /// function is not already taken.
    fn perform_add_generic_nervous_system_function(
        &mut self,
        nervous_system_function: NervousSystemFunction,
    ) -> Result<(), GovernanceError> {
        let id = nervous_system_function.id;

        if nervous_system_function.is_native() {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "Can only add NervousSystemFunction's of \
                                                          GenericNervousSystemFunction function_type",
            ));
        }

        if is_registered_function_id(id, &self.proto.id_to_nervous_system_functions) {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!(
                    "Failed to add NervousSystemFunction. \
                             There is/was already a NervousSystemFunction with id: {id}"
                ),
            ));
        }

        // This validates that it is well-formed, but not the canister targets.
        match ValidGenericNervousSystemFunction::try_from(&nervous_system_function) {
            Ok(valid_function) => {
                let reserved_canisters = self.reserved_canister_targets();
                let target_canister_id = valid_function.target_canister_id;
                let validator_canister_id = valid_function.validator_canister_id;

                if reserved_canisters.contains(&target_canister_id)
                    || reserved_canisters.contains(&validator_canister_id)
                {
                    return Err(GovernanceError::new_with_message(
                        ErrorType::PreconditionFailed,
                        "Cannot add generic nervous system functions that targets sns core canisters, the NNS ledger, or ic00",
                    ));
                }
            }
            Err(msg) => {
                return Err(GovernanceError::new_with_message(
                    ErrorType::PreconditionFailed,
                    msg,
                ));
            }
        }

        self.proto
            .id_to_nervous_system_functions
            .insert(id, nervous_system_function);
        Ok(())
    }

    /// Removes a nervous system function from Governance if the given id for the nervous system
    /// function exists.
    fn perform_remove_generic_nervous_system_function(
        &mut self,
        id: u64,
    ) -> Result<(), GovernanceError> {
        let entry = self.proto.id_to_nervous_system_functions.entry(id);
        match entry {
            Entry::Vacant(_) => Err(GovernanceError::new_with_message(
                ErrorType::NotFound,
                format!(
                    "Failed to remove NervousSystemFunction. There is no NervousSystemFunction with id: {id}"
                ),
            )),
            Entry::Occupied(mut o) => {
                // Insert a deletion marker to signify that there was a NervousSystemFunction
                // with this id at some point, but that it was deleted.
                o.insert(NERVOUS_SYSTEM_FUNCTION_DELETION_MARKER.clone());
                Ok(())
            }
        }
    }

    async fn perform_register_extension(
        &mut self,
        register_extension: RegisterExtension,
    ) -> Result<(), GovernanceError> {
        // Check if SNS extensions are enabled
        if !crate::is_sns_extensions_enabled() {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "SNS extensions are not enabled",
            ));
        }

        let validated_register_extension = validate_register_extension(self, register_extension)
            .await
            .map_err(|err| {
                GovernanceError::new_with_message(
                    ErrorType::InvalidProposal,
                    format!("Invalid RegisterExtension: {err:?}"),
                )
            })?;

        validated_register_extension.execute(self).await?;

        Ok(())
    }

    async fn perform_upgrade_extension(
        &mut self,
        upgrade_extension: UpgradeExtension,
    ) -> Result<(), GovernanceError> {
        // Check if SNS extensions are enabled
        if !crate::is_sns_extensions_enabled() {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "SNS extensions are not enabled",
            ));
        }

        let validated_upgrade_extension = validate_upgrade_extension(self, upgrade_extension)
            .await
            .map_err(|err| {
                GovernanceError::new_with_message(
                    ErrorType::InvalidProposal,
                    format!("Invalid UpgradeExtension: {err:?}"),
                )
            })?;

        validated_upgrade_extension.execute(self).await?;

        Ok(())
    }

    /// Registers a list of Dapp canister ids in the root canister.
    async fn perform_register_dapp_canisters(
        &self,
        register_dapp_canisters: RegisterDappCanisters,
    ) -> Result<(), GovernanceError> {
        let payload = candid::Encode!(&RegisterDappCanistersRequest::from(
            register_dapp_canisters.clone()
        ))
        .map_err(|err| {
            GovernanceError::new_with_message(
                ErrorType::InvalidProposal,
                format!("Could not encode RegisterDappCanistersRequest: {err:?}"),
            )
        })?;
        self.env
            .call_canister(
                self.proto.root_canister_id_or_panic(),
                "register_dapp_canisters",
                payload,
            )
            .await
            // Convert to return type.
            .map(|reply| {
                // This line is to ensure we handle the reply properly if it's ever
                // changed to not be empty.
                match Decode!(&reply, RegisterDappCanistersResponse) {
                    Ok(RegisterDappCanistersResponse {}) => {}
                    Err(_) => log!(ERROR, "Could not decode RegisterDappCanistersResponse!"),
                };

                log!(
                    INFO,
                    "Performed register_dapp_canisters, registering the following canisters: {:?}",
                    &register_dapp_canisters.canister_ids
                );
            })
            .map_err(|err| {
                GovernanceError::new_with_message(
                    ErrorType::External,
                    format!("Canister method call failed: {err:?}"),
                )
            })
    }

    /// Sets the controllers of registered dapp canisters in root.
    /// Dapp canisters can be registered via the register_dapp_canisters proposal.
    async fn perform_deregister_dapp_canisters(
        &self,
        deregister_dapp_canisters: DeregisterDappCanisters,
    ) -> Result<(), GovernanceError> {
        let payload = candid::Encode!(&SetDappControllersRequest::from(
            deregister_dapp_canisters.clone()
        ))
        .map_err(|err| {
            GovernanceError::new_with_message(
                ErrorType::InvalidProposal,
                format!("Could not encode SetDappControllersRequest: {err:?}"),
            )
        })?;
        self.env
            .call_canister(
                self.proto.root_canister_id_or_panic(),
                "set_dapp_controllers",
                payload,
            )
            .await
            // Convert to return type.
            .map_err(|err| {
                GovernanceError::new_with_message(
                    ErrorType::External,
                    format!("Canister method call failed: {err:?}"),
                )
            })
            // Make sure no canisters' controllers failed to be set.
            .and_then(|reply| {
                // This line is to ensure we handle the reply properly if it's ever
                // changed to not be empty.
                match Decode!(&reply, SetDappControllersResponse) {
                    Ok(SetDappControllersResponse { failed_updates }) => {
                        if failed_updates.is_empty() {
                            log!(
                                INFO,
                                "Deregistered the following dapp canisters: {:?}.",
                                deregister_dapp_canisters.canister_ids
                            );
                            Ok(())
                        } else {
                            let message = format!(
                                "When trying to deregister the following dapp canisters: {:?} \n\
                                The following canisters failed to deregister: {:?}",
                                deregister_dapp_canisters.canister_ids, failed_updates
                            );
                            Err(GovernanceError::new_with_message(
                                ErrorType::External,
                                message,
                            ))
                        }
                    }
                    Err(_) => Err(GovernanceError::new_with_message(
                        ErrorType::External,
                        "Could not decode SetDappControllersResponse".to_string(),
                    )),
                }
            })
    }

    // Make a change to the values of Sns Metadata
    fn perform_manage_sns_metadata(
        &mut self,
        manage_sns_metadata: ManageSnsMetadata,
    ) -> Result<(), GovernanceError> {
        let mut sns_metadata = match &self.proto.sns_metadata {
            Some(sns_metadata) => sns_metadata.clone(),
            None => SnsMetadata {
                logo: None,
                url: None,
                name: None,
                description: None,
            },
        };
        let mut log: String = "Updating the following fields of Sns Metadata: \n".to_string();
        if let Some(new_logo) = manage_sns_metadata.logo {
            sns_metadata.logo = Some(new_logo);
            log += "- Logo";
        }
        if let Some(new_url) = manage_sns_metadata.url {
            log += &format!(
                "Url:\n- old value: {}\n- new value: {}",
                sns_metadata.url.unwrap_or_default(),
                new_url
            );
            sns_metadata.url = Some(new_url);
        }
        if let Some(new_name) = manage_sns_metadata.name {
            log += &format!(
                "Name:\n- old value: {}\n- new value: {}",
                sns_metadata.name.unwrap_or_default(),
                new_name
            );
            sns_metadata.name = Some(new_name);
        }
        if let Some(new_description) = manage_sns_metadata.description {
            log += &format!(
                "Description:\n- old value: {}\n- new value: {}",
                sns_metadata.description.unwrap_or_default(),
                new_description
            );
            sns_metadata.description = Some(new_description);
        }
        log!(INFO, "{}", log);
        self.proto.sns_metadata = Some(sns_metadata);
        Ok(())
    }

    /// Executes a (non-native) nervous system function as a result of an adopted proposal.
    async fn perform_execute_generic_nervous_system_function(
        &self,
        call: ExecuteGenericNervousSystemFunction,
    ) -> Result<(), GovernanceError> {
        match self
            .proto
            .id_to_nervous_system_functions
            .get(&call.function_id)
        {
            None => Err(GovernanceError::new_with_message(
                ErrorType::NotFound,
                format!(
                    "There is no generic NervousSystemFunction with id: {}",
                    call.function_id
                ),
            )),
            Some(function) => {
                perform_execute_generic_nervous_system_function_call(
                    &*self.env,
                    function.clone(),
                    call,
                )
                .await
            }
        }
    }

    async fn perform_execute_extension_operation(
        &self,
        execute_extension_operation: ExecuteExtensionOperation,
    ) -> Result<(), GovernanceError> {
        // Check if SNS extensions are enabled
        if !crate::is_sns_extensions_enabled() {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "SNS extensions are not enabled",
            ));
        }

        let validated_operation =
            validate_execute_extension_operation(self, execute_extension_operation).await?;

        // Execute the validated operation
        validated_operation.execute(self).await?;

        Ok(())
    }

    /// Executes a ManageNervousSystemParameters proposal by updating Governance's
    /// NervousSystemParameters
    fn perform_manage_nervous_system_parameters(
        &mut self,
        proposed_params: NervousSystemParameters,
    ) -> Result<(), GovernanceError> {
        // Only set `self.proto.parameters` if "applying" the proposed params to the
        // current params results in valid params
        let new_params = proposed_params.inherit_from(self.nervous_system_parameters_or_panic());

        log!(
            INFO,
            "Setting Governance nervous system params to: {:?}",
            &new_params
        );

        match new_params.validate() {
            Ok(()) => {
                self.proto.parameters = Some(new_params);
                Ok(())
            }

            // Even though proposals are validated when they are first made, this is still
            // possible, because the inner value of a ManageNervousSystemParameters
            // proposal is only valid with respect to the current
            // nervous_system_parameters() at the time when the proposal was first
            // made. If nervous_system_parameters() changed (by another proposal) since
            // the current proposal was first made, the current proposal might have become
            // invalid. Basically, this might occur if there are conflicting (concurrent)
            // proposals, but we expect this to be highly unusual in practice.
            Err(msg) => Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!(
                    "Failed to perform ManageNervousSystemParameters action, proposed \
                        parameters would lead to invalid NervousSystemParameters: {msg}"
                ),
            )),
        }
    }

    pub fn upgrade_proposals_in_progress(&self) -> BTreeSet</* Proposal Id*/ u64> {
        self.proto
            .proposals
            .iter()
            .filter_map(|(id, proposal_data)| {
                let proposal_expiry_time = proposal_data
                    .decided_timestamp_seconds
                    .checked_add(UPGRADE_PROPOSAL_BLOCK_EXPIRY_SECONDS)
                    .unwrap_or_default();
                let proposal_recent_enough = proposal_expiry_time > self.env.now();
                if proposal_data.status() == ProposalDecisionStatus::Adopted
                    && proposal_data.is_upgrade_proposal()
                    && proposal_recent_enough
                {
                    Some(*id)
                } else {
                    None
                }
            })
            .collect::<BTreeSet<_>>()
    }

    /// Executes a UpgradeSnsControlledCanister proposal by calling the root canister
    /// to upgrade an SNS controlled canister.  This does not upgrade "core" SNS canisters
    /// (i.e. Root, Governance, Ledger, Ledger Archives, or Sale)
    async fn perform_upgrade_sns_controlled_canister(
        &mut self,
        proposal_id: u64,
        upgrade: UpgradeSnsControlledCanister,
    ) -> Result<(), GovernanceError> {
        self.check_no_upgrades_in_progress(Some(proposal_id))?;

        let sns_canisters =
            get_all_sns_canisters(&*self.env, self.proto.root_canister_id_or_panic())
                .await
                .map_err(|e| {
                    GovernanceError::new_with_message(
                        ErrorType::External,
                        format!("Could not get list of SNS canisters from SNS Root: {e}"),
                    )
                })?;

        let dapp_canisters: Vec<CanisterId> = sns_canisters
            .dapps
            .iter()
            .map(|x| CanisterId::unchecked_from_principal(*x))
            .collect();

        let target_canister_id = get_canister_id(&upgrade.canister_id)?;
        // Fail if not a registered dapp canister
        if !dapp_canisters.contains(&target_canister_id) {
            return Err(GovernanceError::new_with_message(
                ErrorType::InvalidCommand,
                format!(
                    "UpgradeSnsControlledCanister can only upgrade dapp canisters that are registered \
                    with the SNS root: see Root::register_dapp_canister. Valid targets are: {dapp_canisters:?}"
                ),
            ));
        }

        let mode = upgrade.mode_or_upgrade() as i32;

        let wasm = Wasm::try_from(&upgrade)
            .map_err(|err| GovernanceError::new_with_message(ErrorType::InvalidCommand, err))?;

        self.upgrade_non_root_canister(
            target_canister_id,
            wasm,
            upgrade
                .canister_upgrade_arg
                .unwrap_or_else(|| Encode!().unwrap()),
            CanisterInstallMode::try_from(CanisterInstallModeProto::try_from(mode)?)?,
        )
        .await
    }

    pub(crate) async fn upgrade_non_root_canister(
        &self,
        canister_id: CanisterId,
        wasm: Wasm,
        arg: Vec<u8>,
        mode: CanisterInstallMode,
    ) -> Result<(), GovernanceError> {
        // Serialize upgrade.
        let payload = {
            // We need to stop a canister before we upgrade it. Otherwise it might
            // receive callbacks to calls it made before the upgrade after the
            // upgrade when it might not have the context to parse those usefully.
            //
            // For more details, please refer to the comments above the (definition of the)
            // stop_before_installing field in ChangeCanisterRequest.
            let stop_before_installing = true;

            let mut change_canister_arg =
                ChangeCanisterRequest::new(stop_before_installing, mode, canister_id)
                    .with_arg(arg)
                    .with_mode(mode);

            match wasm {
                Wasm::Bytes(bytes) => {
                    change_canister_arg = change_canister_arg.with_wasm(bytes);
                }
                Wasm::Chunked {
                    wasm_module_hash,
                    store_canister_id,
                    chunk_hashes_list,
                } => {
                    change_canister_arg = change_canister_arg.with_chunked_wasm(
                        wasm_module_hash,
                        store_canister_id,
                        chunk_hashes_list,
                    );
                }
            };

            Encode!(&change_canister_arg).unwrap()
        };

        self.env
            .call_canister(
                self.proto.root_canister_id_or_panic(),
                "change_canister",
                payload,
            )
            .await
            // Convert to return type.
            .map(|_reply| ())
            .map_err(|err| {
                GovernanceError::new_with_message(
                    ErrorType::External,
                    format!("Canister method call failed: {err:?}"),
                )
            })
    }

    /// Used for checking that no upgrades are in progress. Also checks that there are no upgrade proposals in progress except, optionally, one that you pass in as `proposal_id`
    pub fn check_no_upgrades_in_progress(
        &self,
        proposal_id: Option<u64>,
    ) -> Result<(), GovernanceError> {
        let upgrade_proposals_in_progress = self.upgrade_proposals_in_progress();
        if !upgrade_proposals_in_progress.is_subset(&proposal_id.into_iter().collect()) {
            return Err(GovernanceError::new_with_message(
                ErrorType::ResourceExhausted,
                format!(
                    "Another upgrade is currently in progress (proposal IDs {}). \
                    Please, try again later.",
                    upgrade_proposals_in_progress
                        .into_iter()
                        .map(|id| id.to_string())
                        .collect::<Vec<String>>()
                        .join(", ")
                ),
            ));
        }

        if self.proto.pending_version.is_some() {
            return Err(GovernanceError::new_with_message(
                ErrorType::ResourceExhausted,
                format!(
                    "Upgrade lock acquired (expires at {:?}), not upgrading",
                    self.proto
                        .pending_version
                        .as_ref()
                        .map(|p| p.mark_failed_at_seconds)
                ),
            ));
        }

        Ok(())
    }

    /// Best effort to return the deployed version of this SNS.
    ///
    /// Normally, the SNS should always have a deployed version, in which case it is returned.
    /// If this is not the case for whatever reason, this function tries to fetch the running
    /// version, initialize deployed version with it, and return a copy.
    pub async fn get_or_reset_deployed_version(&mut self) -> Result<Version, String> {
        if let Some(deployed_version) = self.proto.deployed_version.clone() {
            return Ok(deployed_version);
        }

        log!(
            ERROR,
            "The SNS does not have a deployed version. Attempting to reset it ..."
        );

        let root_canister_id = self.proto.root_canister_id_or_panic();

        let new_deployed_version = get_running_version(&*self.env, root_canister_id).await?;

        // Re-check that a reentrant call to this function did not yet update the state.
        if let Some(deployed_version) = self.proto.deployed_version.clone() {
            return Ok(deployed_version);
        }

        self.proto
            .deployed_version
            .replace(new_deployed_version.clone());

        Ok(new_deployed_version)
    }

    /// Return `Ok(true)` if the upgrade was completed successfully, return `Ok(false)` if an
    /// upgrade was successfully kicked-off, but its completion is pending.
    async fn perform_upgrade_to_next_sns_version_legacy(
        &mut self,
        proposal_id: u64,
    ) -> Result<bool, GovernanceError> {
        self.check_no_upgrades_in_progress(Some(proposal_id))?;

        let current_version = self.get_or_reset_deployed_version().await.map_err(|err| {
            GovernanceError::new_with_message(
                ErrorType::External,
                format!("Could not execute proposal: {err}"),
            )
        })?;

        let root_canister_id = self.proto.root_canister_id_or_panic();

        let UpgradeSnsParams {
            next_version,
            canister_type_to_upgrade,
            new_wasm_hash,
            canister_ids_to_upgrade,
        } = get_upgrade_params(&*self.env, root_canister_id, &current_version)
            .await
            .map_err(|err| {
                GovernanceError::new_with_message(
                    ErrorType::InvalidProposal,
                    format!("Could not execute proposal: {err}"),
                )
            })?;

        self.push_to_upgrade_journal(upgrade_journal_entry::UpgradeStarted::from_proposal(
            current_version.clone(),
            next_version.clone(),
            ProposalId { id: proposal_id },
        ));

        let target_wasm = get_wasm(&*self.env, new_wasm_hash.to_vec(), canister_type_to_upgrade)
            .await
            .map_err(|e| {
                GovernanceError::new_with_message(
                    ErrorType::External,
                    format!("Could not execute proposal: {e}"),
                )
            })?
            .wasm;

        let target_is_root = canister_ids_to_upgrade.contains(&root_canister_id);

        if target_is_root {
            upgrade_canister_directly(
                &*self.env,
                root_canister_id,
                target_wasm,
                Encode!().unwrap(),
            )
            .await?;
        } else {
            for target_canister_id in canister_ids_to_upgrade {
                self.upgrade_non_root_canister(
                    target_canister_id,
                    Wasm::Bytes(target_wasm.clone()),
                    Encode!().unwrap(),
                    CanisterInstallMode::Upgrade,
                )
                .await?;
            }
        }

        // A canister upgrade has been successfully kicked-off. Set the pending upgrade-in-progress
        // field so that Governance's run_periodic_tasks logic can check on the status of
        // this upgrade.
        self.proto.pending_version = Some(PendingVersion {
            target_version: Some(next_version),
            mark_failed_at_seconds: self.env.now() + 5 * 60,
            checking_upgrade_lock: 0,
            proposal_id: Some(proposal_id),
        });

        log!(
            INFO,
            "Successfully kicked off upgrade for SNS canister {:?}",
            canister_type_to_upgrade,
        );

        Ok(false)
    }

    async fn upgrade_sns_framework_canister(
        &mut self,
        new_wasm_hash: Vec<u8>,
        canister_type_to_upgrade: SnsCanisterType,
    ) -> Result<(), GovernanceError> {
        let root_canister_id = self.proto.root_canister_id()?;

        let target_wasm = get_wasm(&*self.env, new_wasm_hash.to_vec(), canister_type_to_upgrade)
            .await
            .map_err(|e| {
                GovernanceError::new_with_message(
                    ErrorType::External,
                    format!("Could not get wasm for upgrade: {e}"),
                )
            })?
            .wasm;

        let target_is_root = canister_type_to_upgrade == SnsCanisterType::Root;

        if target_is_root {
            upgrade_canister_directly(
                &*self.env,
                root_canister_id,
                target_wasm,
                Encode!().unwrap(),
            )
            .await?;
        } else {
            let canister_ids_to_upgrade =
                get_canisters_to_upgrade(&*self.env, root_canister_id, canister_type_to_upgrade)
                    .await
                    .map_err(|e| {
                        GovernanceError::new_with_message(
                            ErrorType::External,
                            format!("Could not get list of SNS canisters from SNS Root: {e}"),
                        )
                    })?;
            for target_canister_id in canister_ids_to_upgrade {
                self.upgrade_non_root_canister(
                    target_canister_id,
                    Wasm::Bytes(target_wasm.clone()),
                    Encode!().unwrap(),
                    CanisterInstallMode::Upgrade,
                )
                .await?;
            }
        }

        log!(
            INFO,
            "Successfully kicked off upgrade for SNS canister {:?}",
            canister_type_to_upgrade,
        );

        Ok(())
    }

    fn sns_treasury_icp_subaccount(&self) -> Option<Subaccount> {
        None
    }

    fn sns_treasury_sns_token_subaccount(&self) -> Option<Subaccount> {
        // See ic_sns_init::distributions::FractionalDeveloperVotingPower.insert_treasury_accounts
        let treasury_subaccount = compute_distribution_subaccount_bytes(
            self.env.canister_id().get(),
            TREASURY_SUBACCOUNT_NONCE,
        );
        Some(treasury_subaccount)
    }

    async fn perform_transfer_sns_treasury_funds(
        &mut self,
        proposal_id: u64, // This is just to control concurrency.
        valuation: Result<Valuation, GovernanceError>,
        transfer: &TransferSnsTreasuryFunds,
    ) -> Result<(), GovernanceError> {
        // Only execute one proposal of this type at a time.
        thread_local! {
            static IN_PROGRESS_PROPOSAL_ID: RefCell<Option<u64>> = const { RefCell::new(None) };
        }
        let release_on_drop = acquire(&IN_PROGRESS_PROPOSAL_ID, proposal_id);
        if let Err(already_in_progress_proposal_id) = release_on_drop {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!(
                    "Another TransferSnsTreasuryFunds proposal (ID = {already_in_progress_proposal_id}) is already in progress.",
                ),
            ));
        }

        transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err(
            transfer,
            valuation?,
            self.proto.proposals.values(),
            self.env.now(),
        )?;

        let to = Account {
            owner: transfer
                .to_principal
                .expect("Expected transfer to have a target principal")
                .0,
            subaccount: transfer.to_subaccount.as_ref().map(|s| {
                bytes_to_subaccount(&s.subaccount[..])
                    .expect("Couldn't transform transfer.subaccount to Subaccount")
            }),
        };
        match transfer.from_treasury() {
            TransferFrom::IcpTreasury => self
                .nns_ledger
                .transfer_funds(
                    transfer.amount_e8s,
                    NNS_DEFAULT_TRANSFER_FEE.get_e8s(),
                    self.sns_treasury_icp_subaccount(),
                    to,
                    transfer.memo.unwrap_or(0),
                )
                .await
                .map(|_| ())
                .map_err(|e| {
                    GovernanceError::new_with_message(
                        ErrorType::External,
                        format!("Error making ICP treasury transfer: {e}"),
                    )
                }),
            TransferFrom::SnsTokenTreasury => {
                let transaction_fee_e8s = self.transaction_fee_e8s_or_panic();

                self.ledger
                    .transfer_funds(
                        transfer.amount_e8s,
                        transaction_fee_e8s,
                        self.sns_treasury_sns_token_subaccount(),
                        to,
                        transfer.memo.unwrap_or(0),
                    )
                    .await
                    .map(|_| ())
                    .map_err(|e| {
                        GovernanceError::new_with_message(
                            ErrorType::External,
                            format!("Error making SNS Token treasury transfer: {e}"),
                        )
                    })
            }
            TransferFrom::Unspecified => Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "Invalid 'from_treasury' in transfer.",
            )),
        }
    }

    async fn perform_mint_sns_tokens(
        &mut self,
        mint: MintSnsTokens,
    ) -> Result<(), GovernanceError> {
        let to = Account {
            owner: mint
                .to_principal
                .ok_or(GovernanceError::new_with_message(
                    ErrorType::InvalidProposal,
                    "Expected mint to have a target principal",
                ))?
                .0,
            subaccount: mint
                .to_subaccount
                .as_ref()
                .map(|s| bytes_to_subaccount(&s.subaccount[..]))
                .transpose()?,
        };
        let amount_e8s = mint.amount_e8s.ok_or(GovernanceError::new_with_message(
            ErrorType::InvalidProposal,
            "Expected MintSnsTokens to have an an amount_e8s",
        ))?;
        self.ledger
            .transfer_funds(amount_e8s, 0, None, to, mint.memo())
            .await?;
        Ok(())
    }

    async fn perform_manage_ledger_parameters(
        &mut self,
        proposal_id: u64,
        manage_ledger_parameters: ManageLedgerParameters,
    ) -> Result<(), GovernanceError> {
        self.check_no_upgrades_in_progress(Some(proposal_id))?;

        let current_version = self.get_or_reset_deployed_version().await.map_err(|err| {
            GovernanceError::new_with_message(
                ErrorType::External,
                format!("Could not execute proposal: {err}"),
            )
        })?;

        let ledger_canister_id = self.proto.ledger_canister_id_or_panic();

        let ledger_canister_info = self.env
            .call_canister(
                CanisterId::ic_00(),
                "canister_info",
                candid::encode_one(
                    CanisterInfoRequest::new(
                        ledger_canister_id,
                        Some(1),
                    )
                ).map_err(|e| GovernanceError::new_with_message(ErrorType::External, format!("Could not execute proposal. Error encoding canister_info request.\n{e}")))?
            )
            .await
            .map(|b| {
                candid::decode_one::<CanisterInfoResponse>(&b)
                .map_err(|e| GovernanceError::new_with_message(ErrorType::External, format!("Could not execute proposal. Error decoding canister_info response.\n{e}")))
            })
            .map_err(|err: (Option<i32>, String)| GovernanceError::new_with_message(ErrorType::External, format!("Canister method call canister_info failed: {err:?}")))??;

        let ledger_canister_info_version_number_before_upgrade: u64 =
            ledger_canister_info
            .changes()
            .last().ok_or(GovernanceError::new_with_message(ErrorType::External, "Could not execute proposal. Error finding current ledger canister_info version number".to_string()))?
            .canister_version();

        let ledger_wasm = get_wasm(
            &*self.env,
            current_version.ledger_wasm_hash.clone(),
            SnsCanisterType::Ledger,
        )
        .await
        .map_err(|e| {
            GovernanceError::new_with_message(
                ErrorType::External,
                format!("Could not execute proposal. Error getting ledger canister wasm: {e}"),
            )
        })?
        .wasm;

        use ic_icrc1_ledger::{LedgerArgument, UpgradeArgs};
        let ledger_upgrade_arg = candid::encode_one(Some(LedgerArgument::Upgrade(Some(
            UpgradeArgs::from(manage_ledger_parameters.clone()),
        ))))
        .unwrap();

        self.upgrade_non_root_canister(
            ledger_canister_id,
            Wasm::Bytes(ledger_wasm),
            ledger_upgrade_arg,
            CanisterInstallMode::Upgrade,
        )
        .await?;

        // If this operation takes 5 minutes, there is very likely a real failure, and other intervention will
        // be required
        let mark_failed_at_seconds = self.env.now() + 5 * 60;

        loop {
            let ledger_canister_info = self.env
                .call_canister(
                    CanisterId::ic_00(),
                    "canister_info",
                    candid::encode_one(
                        CanisterInfoRequest::new(
                            ledger_canister_id,
                            Some(20), // Get enough to ensure we did not miss the relevant change
                        )
                    ).map_err(|e| GovernanceError::new_with_message(ErrorType::External, format!("Could not check if ledger upgrade succeeded. Error encoding canister_info request.\n{e}")))?
                )
                .await
                .map(|b| {
                    candid::decode_one::<CanisterInfoResponse>(&b)
                        .map_err(|e| GovernanceError::new_with_message(ErrorType::External, format!("Could not check if ledger upgrade succeeded. Error decoding canister_info response.\n{e}")))
                })
                .map_err(|e| GovernanceError::new_with_message(ErrorType::External, format!("Could not check if ledger upgrade succeeded. Canister method call canister_info failed: {e:?}")))??;

            for canister_change in ledger_canister_info.changes().iter().rev() {
                if canister_change.canister_version()
                    > ledger_canister_info_version_number_before_upgrade
                    && let CanisterChangeDetails::CanisterCodeDeployment(code_deployment) =
                        canister_change.details()
                    && let CanisterInstallMode::Upgrade = code_deployment.mode()
                    && code_deployment.module_hash()[..] == current_version.ledger_wasm_hash[..]
                {
                    // success
                    // update nervous-system-parameters transaction_fee if the fee is changed.
                    if let Some(nervous_system_parameters) = self.proto.parameters.as_mut()
                        && let Some(transfer_fee) = manage_ledger_parameters.transfer_fee
                    {
                        nervous_system_parameters.transaction_fee_e8s = Some(transfer_fee);
                    }
```

**File:** rs/sns/governance/src/types.rs (L610-614)
```rust
        if neuron_minimum_stake_e8s <= transaction_fee_e8s {
            Err(format!(
                "NervousSystemParameters.neuron_minimum_stake_e8s ({neuron_minimum_stake_e8s}) must be greater than \
                NervousSystemParameters.transaction_fee_e8s ({neuron_minimum_stake_e8s})"
            ))
```
