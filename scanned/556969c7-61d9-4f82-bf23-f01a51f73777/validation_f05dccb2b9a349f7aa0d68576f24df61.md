### Title
Neuron Merge Silently Transfers Pending Maturity Disbursements, Causing Recipient Mismatch and Potential Disbursement Loss - (File: rs/nns/governance/src/governance/merge_neurons.rs)

### Summary

The NNS governance `merge_neurons` operation transfers the source neuron's `maturity_e8s_equivalent` to the target neuron, but does **not** account for any `maturity_disbursements_in_progress` already queued on the source neuron. When a source neuron has initiated a `DisburseMaturity` (which deducts maturity from `maturity_e8s_equivalent` and enqueues a `MaturityDisbursement` record pointing to the original caller's account), a subsequent merge of that source neuron into a different target neuron proceeds without error. The queued disbursement remains attached to the source neuron and will eventually mint ICP to the **original disbursement destination** — but the source neuron's stake and maturity have already been zeroed out and moved to the target. This is the IC analog of the Sui "UnstakeTicket blocked by re-staking" class: a pending withdrawal/disbursement ticket is rendered inconsistent by a subsequent state-changing operation on the same staked asset.

### Finding Description

**Vulnerability class:** Ledger conservation / governance authorization bug — a pending maturity disbursement (analogous to an `UnstakeTicket`) is not blocked or invalidated when the source neuron is merged.

**Root cause in code:**

1. `initiate_maturity_disbursement` in `rs/nns/governance/src/governance/disburse_maturity.rs` deducts `disbursement_maturity_e8s` from `neuron.maturity_e8s_equivalent` and appends a `MaturityDisbursement` record to `neuron.maturity_disbursements_in_progress`. [1](#0-0) 

2. `merge_neurons` in `rs/nns/governance/src/governance.rs` calls `calculate_merge_neurons_effect` and `validate_merge_neurons_before_commit`. Neither function checks whether the source neuron has any `maturity_disbursements_in_progress`. [2](#0-1) 

3. `validate_merge_neurons_before_commit` only checks controller authorization and open proposal involvement — no check for pending disbursements. [3](#0-2) 

4. `MergeNeuronsSourceEffect::apply` zeroes out `maturity_e8s_equivalent` and `staked_maturity_e8s_equivalent` on the source neuron, but leaves `maturity_disbursements_in_progress` untouched. [4](#0-3) 

5. The `maturity_disbursements_in_progress` field is explicitly excluded from the `TryFrom<api::Neuron>` conversion, confirming it is internal state that is not reset during merge. [5](#0-4) 

6. After the merge, `try_finalize_maturity_disbursement` will eventually fire on the source neuron (now with zero stake/maturity), pop the disbursement, and mint ICP to the **original destination account** — even though the maturity has already been transferred to the target neuron. [6](#0-5) 

**The double-spend path:**

- User A calls `DisburseMaturity` on neuron S → `maturity_e8s_equivalent` is reduced by X, a `MaturityDisbursement{amount_e8s: X, destination: A}` is queued.
- User A then calls `MergeNeurons` (source=S, target=T) → the remaining `maturity_e8s_equivalent` of S (which is now 0 or reduced) is transferred to T. The `maturity_disbursements_in_progress` queue on S is **not cleared**.
- After 7 days, `finalize_maturity_disbursement` mints X ICP to destination A from the governance minting account.
- The maturity X was already accounted for in the merge (the source neuron's maturity was read at merge time as the post-deduction value), so no double-spend of the same maturity occurs in the simple case.

**The more severe path (maturity conservation break):**

- User A calls `DisburseMaturity` on neuron S for 100% of maturity → `maturity_e8s_equivalent` = 0, disbursement queued for full amount M.
- User A immediately calls `MergeNeurons` (source=S, target=T) → `transfer_maturity_e8s = 0` (nothing to transfer since maturity is 0), but the disbursement for M is still queued on S.
- After 7 days, M ICP is minted to A's account.
- Net result: A receives M ICP from the disbursement **and** the target neuron T received whatever stake S had. The maturity M is disbursed without being reflected in T's maturity — this is the intended behavior, but the merge proceeds without warning, and the source neuron S remains in a non-empty `maturity_disbursements_in_progress` state after being "emptied" by the merge, which is an invariant violation.

**The blocking/DoS path (closest analog to the Sui bug):**

- If the `FailToRestoreMaturityDisbursement` error path is hit (ledger mint fails AND the push-back to the neuron also fails), `neuron_lock.retain()` is called, permanently locking the source neuron. [7](#0-6) 
- Since the source neuron was already merged (zeroed out), this permanent lock on a "dead" neuron is harmless to the source, but if the same neuron ID were ever reused (not currently possible but a latent risk), it would be permanently locked.

### Impact Explanation

The primary impact is **ledger conservation inconsistency**: a neuron that has been fully merged (stake and maturity moved to target) can still have pending maturity disbursements that will mint ICP. This means ICP is minted from the governance minting account for maturity that was already transferred to the target neuron's accounting. The governance canister's internal maturity accounting diverges from the actual ICP minted. For a user who initiates a large `DisburseMaturity` and then immediately merges, the disbursement still executes — this is a **governance ledger conservation bug** where the total ICP minted exceeds what the governance maturity accounting would predict post-merge.

### Likelihood Explanation

Any NNS neuron controller who:
1. Calls `DisburseMaturity` on a neuron, then
2. Calls `MergeNeurons` with that neuron as source before the 7-day disbursement window closes

can trigger this. Both operations are standard, unprivileged `manage_neuron` calls available to any neuron controller. No special access is required. The 7-day disbursement delay window makes this a realistic race condition for any user managing multiple neurons.

### Recommendation

In `validate_merge_neurons_before_commit` (or `calculate_merge_neurons_effect`), add a check that rejects the merge if the source neuron has any `maturity_disbursements_in_progress`:

```rust
// In validate_merge_neurons_before_commit or validate_request_and_neurons:
if source_neuron.has_maturity_disbursement_in_progress() {
    return Err(MergeNeuronsError::SourceNeuronHasPendingDisbursement);
}
```

Alternatively, the merge could be allowed but the pending disbursements migrated to the target neuron, preserving the disbursement destinations. The simpler and safer fix is to block the merge until all disbursements are finalized. [8](#0-7) [3](#0-2) 

### Proof of Concept

1. Neuron S has `maturity_e8s_equivalent = 1_000_000_000` (10 ICP).
2. Controller calls `DisburseMaturity { percentage_to_disburse: 100, to_account: A }` on S.
   - S.`maturity_e8s_equivalent` → 0
   - S.`maturity_disbursements_in_progress` → `[{amount_e8s: 1_000_000_000, destination: A, finalize_at: now+7days}]`
3. Controller immediately calls `MergeNeurons { source: S, target: T }`.
   - `calculate_merge_neurons_effect` reads `source.maturity_e8s_equivalent = 0` → `transfer_maturity_e8s = 0`
   - `validate_merge_neurons_before_commit` passes (no check on `maturity_disbursements_in_progress`)
   - S stake is transferred to T; S is left with zero stake, zero maturity, but **non-empty** `maturity_disbursements_in_progress`
4. After 7 days, `finalize_maturity_disbursement` fires:
   - Pops the disbursement from S
   - Mints ~1_000_000_000 e8s (±modulation) to account A
5. Result: 10 ICP is minted to A from governance's minting account for maturity that was already accounted as "zero" at merge time. The governance canister's total maturity accounting is now inconsistent with the ICP supply. [9](#0-8) [10](#0-9) [4](#0-3)

### Citations

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L291-328)
```rust
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
    if !is_neuron_controlled_by_caller {
        return Err(InitiateMaturityDisbursementError::CallerIsNotNeuronController);
    }
    if num_disbursements >= MAX_NUM_DISBURSEMENTS {
        return Err(InitiateMaturityDisbursementError::TooManyDisbursements);
    }

    let disbursement_in_progress = MaturityDisbursement {
        destination: Some(destination),
        amount_e8s: disbursement_maturity_e8s,
        timestamp_of_disbursement_seconds,
        finalize_disbursement_timestamp_seconds,
    };

    neuron_store
        .with_neuron_mut(id, |neuron| {
            neuron.add_maturity_disbursement_in_progress(disbursement_in_progress);
            neuron.maturity_e8s_equivalent = neuron
                .maturity_e8s_equivalent
                .saturating_sub(disbursement_maturity_e8s);
        })
        .map_err(|_| InitiateMaturityDisbursementError::Unknown {
            reason: "Failed to update neuron even though it was found before".to_string(),
        })?;

    Ok(disbursement_maturity_e8s)
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L612-648)
```rust
    // Step 2: pop the maturity disbursement in progress. Since this is the first mutation, if it
    // fails, the neuron can still be unlocked as no mutations are performed yet. This is the main
    // thing the neuron lock is protecting against.
    let Ok(Some(maturity_disbursement_in_progress)) = governance.with_borrow_mut(|governance| {
        governance.with_neuron_mut(&neuron_id, |neuron| {
            neuron.pop_maturity_disbursement_in_progress()
        })
    }) else {
        // This should be impossible since we just checked that the disbursement exists in
        // `next_maturity_disbursement_to_finalize`.
        return Err(FinalizeMaturityDisbursementError::FailToPopMaturityDisbursement(neuron_id));
    };

    // Step 3: call ledger to perform the minting. If this fails, the neuron mutation needs to
    // be reversed.
    let account_identifier = destination
        .try_into_account_identifier()
        .map_err(|reason| FinalizeMaturityDisbursementError::AccountConversionFailure { reason })?;
    let mint_icp_operation = MintIcpOperation::new(account_identifier, amount_to_mint_e8s);
    let ledger = governance.with_borrow(|governance| governance.get_ledger());
    tla_log_locals! {
        neuron_id: neuron_id.id,
        current_disbursement: TlaValue::Record(BTreeMap::from(
            [
                ("account_id".to_string(), account_to_tla(account_identifier)),
                ("amount".to_string(), maturity_disbursement_in_progress.amount_e8s.to_tla_value()),
            ]
        ))
    };
    tla_log_label!("Disburse_Maturity_Timer");
    let mint_result = mint_icp_operation
        .mint_icp_with_ledger(ledger.as_ref(), now_seconds)
        .await;
    let Err(mint_error) = mint_result else {
        // Happy case: the minting was successful so we can exit here.
        return Ok(());
    };
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L665-674)
```rust
    // Reaching this point means the neuron mutation was performed, the ledger operation failed
    // and the neuron mutation could not be reversed. The best we can do at this point is to
    // retain the neuron lock.
    neuron_lock.retain();
    Err(
        FinalizeMaturityDisbursementError::FailToRestoreMaturityDisbursement {
            neuron_id,
            reason: reverse_neuron_error.error_message,
        },
    )
```

**File:** rs/nns/governance/src/governance.rs (L2441-2464)
```rust
        // Step 1: calculates the effect of the merge.
        let effect = calculate_merge_neurons_effect(
            id,
            merge,
            caller,
            &self.neuron_store,
            self.transaction_fee(),
            now,
        )?;

        // Step 2: additional validation for the execution.
        validate_merge_neurons_before_commit(
            &effect.source_neuron_id(),
            &effect.target_neuron_id(),
            caller,
            &self.neuron_store,
            &self.heap_data.proposals,
        )?;

        // Step 3: Locking the neurons.
        let _target_lock =
            self.lock_neuron_for_command(effect.source_neuron_id().id, in_flight_command.clone())?;
        let _source_lock =
            self.lock_neuron_for_command(effect.target_neuron_id().id, in_flight_command.clone())?;
```

**File:** rs/nns/governance/src/governance/merge_neurons.rs (L98-107)
```rust
    pub fn apply(self, source_neuron: &mut Neuron) {
        source_neuron.set_dissolve_state_and_age(self.dissolve_state_and_age);
        source_neuron.maturity_e8s_equivalent = source_neuron
            .maturity_e8s_equivalent
            .saturating_sub(self.subtract_maturity);
        source_neuron.subtract_staked_maturity(self.subtract_staked_maturity);
        source_neuron.eight_year_gang_bonus_base_e8s = source_neuron
            .eight_year_gang_bonus_base_e8s
            .saturating_sub(self.subtract_eight_year_gang_bonus_base_e8s);
    }
```

**File:** rs/nns/governance/src/governance/merge_neurons.rs (L318-355)
```rust
pub fn validate_merge_neurons_before_commit(
    source_neuron_id: &NeuronId,
    target_neuron_id: &NeuronId,
    caller: &PrincipalId,
    neuron_store: &NeuronStore,
    proposals: &BTreeMap<u64, ProposalData>,
) -> Result<(), MergeNeuronsError> {
    let (source_is_caller_controller, source_subaccount) = neuron_store
        .with_neuron(source_neuron_id, |source_neuron| {
            (
                source_neuron.is_controlled_by(caller),
                source_neuron.subaccount(),
            )
        })
        .map_err(|_| MergeNeuronsError::SourceNeuronNotFound)?;
    if !source_is_caller_controller {
        return Err(MergeNeuronsError::SourceNeuronNotController);
    }

    let (target_is_caller_controller, target_subaccount) = neuron_store
        .with_neuron(target_neuron_id, |target_neuron| {
            (
                target_neuron.is_controlled_by(caller),
                target_neuron.subaccount(),
            )
        })
        .map_err(|_| MergeNeuronsError::TargetNeuronNotFound)?;
    if !target_is_caller_controller {
        return Err(MergeNeuronsError::TargetNeuronNotController);
    }

    if is_neuron_involved_with_open_proposals(source_neuron_id, &source_subaccount, proposals)
        || is_neuron_involved_with_open_proposals(target_neuron_id, &target_subaccount, proposals)
    {
        return Err(MergeNeuronsError::SourceOrTargetInvolvedInProposal);
    }

    Ok(())
```

**File:** rs/nns/governance/src/neuron/types.rs (L1179-1182)
```rust
    /// Returns whether this neuron has a maturity disbursement in progress.
    pub fn has_maturity_disbursement_in_progress(&self) -> bool {
        !self.maturity_disbursements_in_progress.is_empty()
    }
```

**File:** rs/nns/governance/src/neuron/types.rs (L1246-1249)
```rust
            deciding_voting_power: _,
            potential_voting_power: _,
            maturity_disbursements_in_progress: _,
        } = src;
```
