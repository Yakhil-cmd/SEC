### Title
NNS Governance `merge_neurons` Hotkey Authorization Inconsistency — (`rs/nns/governance/src/governance/merge_neurons.rs`)

### Summary
The `merge_neurons` execution path in the NNS Governance canister applies two sequential authorization checks with contradictory standards. Phase 1 (`calculate_merge_neurons_effect`) explicitly permits hotkeys via `is_authorized_to_simulate_manage_neuron`, while Phase 2 (`validate_merge_neurons_before_commit`) silently escalates the requirement to controller-only via `is_controlled_by`. A hotkey that is explicitly authorized in Phase 1 will always be rejected in Phase 2 with a misleading error. This mirrors the external report's pattern: an approved delegate is permitted to perform a related operation (simulate the merge) but is blocked from executing the actual operation.

### Finding Description

`merge_neurons` in `rs/nns/governance/src/governance.rs` runs two sequential validation steps:

**Step 1** — `calculate_merge_neurons_effect` calls `validate_request_and_neurons`, which checks:

```rust
let source_is_caller_authorized =
    source_neuron.is_authorized_to_simulate_manage_neuron(caller);
```

`is_authorized_to_simulate_manage_neuron` delegates to `is_hotkey_or_controller`:

```rust
pub(crate) fn is_authorized_to_simulate_manage_neuron(&self, principal: &PrincipalId) -> bool {
    self.is_hotkey_or_controller(principal)
}
```

A hotkey passes this check. [1](#0-0) 

**Step 2** — `validate_merge_neurons_before_commit` then checks:

```rust
let (source_is_caller_controller, source_subaccount) = neuron_store
    .with_neuron(source_neuron_id, |source_neuron| {
        (source_neuron.is_controlled_by(caller), ...)
    })?;
if !source_is_caller_controller {
    return Err(MergeNeuronsError::SourceNeuronNotController);
}
```

`is_controlled_by` only returns `true` for the controller, never a hotkey. [2](#0-1) 

The two steps are called back-to-back in `merge_neurons`:

```rust
let effect = calculate_merge_neurons_effect(id, merge, caller, ...)?;  // hotkeys pass
validate_merge_neurons_before_commit(..., caller, ...)?;                // hotkeys fail
``` [3](#0-2) 

The error variants confirm the asymmetry: `SourceNeuronNotHotKeyOrController` is returned from Phase 1 when neither role is present, while `SourceNeuronNotController` is returned from Phase 2 when the caller is a hotkey but not the controller. [4](#0-3) 

Meanwhile, `simulate_merge_neurons` runs only Phase 1 and succeeds for hotkeys, making the inconsistency observable: a hotkey can simulate a merge but cannot execute it. [5](#0-4) 

The same hotkey is also explicitly permitted to:
- Vote and submit proposals (`is_authorized_to_vote` → `is_hotkey_or_controller`) [6](#0-5) 
- Join or leave the Neurons' Fund (`JoinCommunityFund | LeaveCommunityFund` → `is_hotkey_or_controller`) [7](#0-6) 

These are operations with comparable or greater financial consequence than merging two neurons.

### Impact Explanation

A neuron controller who has delegated a hotkey to manage their neuron's governance participation cannot have that hotkey execute neuron merges. The hotkey passes Phase 1 (effect calculation succeeds, ledger interactions may begin), then is rejected at Phase 2 with an error message ("Source neuron must be owned by the caller") that contradicts the Phase 1 authorization. This blocks legitimate neuron management delegation, prevents optimization of dissolve delay and voting power through merges, and creates a confusing authorization model where simulation succeeds but execution fails for the same caller.

### Likelihood Explanation

Any registered hotkey of any NNS neuron can trigger this inconsistency by submitting a `manage_neuron` call with `Command::Merge`. No special setup is required beyond being a hotkey, which is a standard, unprivileged role explicitly granted by the neuron controller. The entry path is a normal ingress message to the NNS Governance canister.

### Recommendation

Change `validate_merge_neurons_before_commit` to use `is_hotkey_or_controller` (or equivalently `is_authorized_to_simulate_manage_neuron`) instead of `is_controlled_by` for both the source and target neuron checks, making Phase 2 consistent with Phase 1 of the same operation:

```rust
// In validate_merge_neurons_before_commit:
let source_is_authorized = neuron_store
    .with_neuron(source_neuron_id, |n| n.is_hotkey_or_controller(caller))?;
if !source_is_authorized {
    return Err(MergeNeuronsError::SourceNeuronNotHotKeyOrController);
}
```

Alternatively, if controller-only execution is the intended design, Phase 1 (`validate_request_and_neurons`) should also enforce `is_controlled_by` so that hotkeys are rejected early with a consistent error, and `simulate_merge_neurons` should document that it uses a relaxed authorization model.

### Proof of Concept

1. Create two NNS neurons (A and B) with the same controller `C` and a shared hotkey `H`.
2. As `H`, call `simulate_manage_neuron` with `Command::Merge { source_neuron_id: A }` targeting B → **succeeds** (Phase 1 passes, `is_hotkey_or_controller` returns `true`).
3. As `H`, call `manage_neuron` with the same `Command::Merge` → **fails at Phase 2** with `GovernanceError { error_type: NotAuthorized, error_message: "Source neuron must be owned by the caller" }`.
4. As `C` (the controller), repeat step 3 → **succeeds**.

The test `test_calculate_effect_source_or_target_not_authorized` in `rs/nns/governance/src/governance/merge_neurons.rs` already documents that `SourceNeuronNotHotKeyOrController` is the Phase 1 error, confirming hotkeys are expected to pass Phase 1. [8](#0-7)

### Citations

**File:** rs/nns/governance/src/governance/merge_neurons.rs (L173-234)
```rust
            MergeNeuronsError::SourceNeuronNotHotKeyOrController => {
                GovernanceError::new_with_message(
                    ErrorType::NotAuthorized,
                    "Caller must be hotkey or controller of the source neuron",
                )
            }
            MergeNeuronsError::TargetNeuronNotHotKeyOrController => {
                GovernanceError::new_with_message(
                    ErrorType::NotAuthorized,
                    "Caller must be hotkey or controller of the target neuron",
                )
            }
            MergeNeuronsError::SourceNeuronSpawning => GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "Can't perform operation on neuron: Source neuron is spawning.",
            ),
            MergeNeuronsError::TargetNeuronSpawning => GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "Can't perform operation on neuron: Target neuron is spawning.",
            ),
            MergeNeuronsError::SourceNeuronDissolving => GovernanceError::new_with_message(
                ErrorType::RequiresNotDissolving,
                "Only two non-dissolving neurons with a dissolve delay greater than 0 \
                can be merged.",
            ),
            MergeNeuronsError::TargetNeuronDissolving => GovernanceError::new_with_message(
                ErrorType::RequiresNotDissolving,
                "Only two non-dissolving neurons with a dissolve delay greater than 0 \
                can be merged.",
            ),
            MergeNeuronsError::SourceNeuronInNeuronsFund => GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "Cannot merge neurons that have been dedicated to the Neurons' Fund",
            ),
            MergeNeuronsError::TargetNeuronInNeuronsFund => GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "Cannot merge neurons that have been dedicated to the Neurons' Fund",
            ),
            MergeNeuronsError::NeuronManagersNotSame => GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "ManageNeuron following of source and target does not match",
            ),
            MergeNeuronsError::KycVerifiedNotSame => GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "Source neuron's kyc_verified field does not match target",
            ),
            MergeNeuronsError::NotForProfitNotSame => GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "Source neuron's not_for_profit field does not match target",
            ),
            MergeNeuronsError::NeuronTypeNotSame => GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "Source neuron's neuron_type field does not match target",
            ),
            MergeNeuronsError::SourceNeuronNotController => GovernanceError::new_with_message(
                ErrorType::NotAuthorized,
                "Source neuron must be owned by the caller",
            ),
            MergeNeuronsError::TargetNeuronNotController => GovernanceError::new_with_message(
                ErrorType::NotAuthorized,
                "Target neuron must be owned by the caller",
            ),
```

**File:** rs/nns/governance/src/governance/merge_neurons.rs (L325-347)
```rust
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
```

**File:** rs/nns/governance/src/governance/merge_neurons.rs (L511-535)
```rust
        .with_neuron(&source_neuron_id, |source_neuron| {
            let source_neuron_to_merge = ValidSourceNeuron::try_new(source_neuron, now_seconds);
            let source_is_caller_authorized =
                source_neuron.is_authorized_to_simulate_manage_neuron(caller);
            let source_is_not_spawning = source_neuron.state(now_seconds) != NeuronState::Spawning;
            let source_is_not_in_neurons_fund = !source_neuron.is_a_neurons_fund_member();
            let source_neuron_managers = source_neuron.neuron_managers();
            let source_kyc_verified = source_neuron.kyc_verified;
            let source_not_for_profit = source_neuron.not_for_profit;
            let source_neuron_type = source_neuron.neuron_type;

            (
                source_neuron_to_merge,
                source_is_caller_authorized,
                source_is_not_spawning,
                source_is_not_in_neurons_fund,
                source_neuron_managers,
                source_kyc_verified,
                source_not_for_profit,
                source_neuron_type,
            )
        })
        .map_err(|_| MergeNeuronsError::SourceNeuronNotFound)?;
    if !source_is_caller_authorized {
        return Err(MergeNeuronsError::SourceNeuronNotHotKeyOrController);
```

**File:** rs/nns/governance/src/governance/merge_neurons.rs (L762-800)
```rust
    #[test]
    fn test_calculate_effect_source_or_target_not_authorized() {
        let mut neuron_store = NeuronStore::new(BTreeMap::new());
        let neuron_not_authorized = create_model_neuron_builder(1)
            .with_controller(PrincipalId::new_user_test_id(2))
            .build();
        neuron_store.add_neuron(neuron_not_authorized).unwrap();
        neuron_store
            .add_neuron(create_model_neuron_builder(2).build())
            .unwrap();

        // Source not authorized.
        let error = calculate_merge_neurons_effect(
            &NeuronId { id: 2 },
            &Merge {
                source_neuron_id: Some(NeuronId { id: 1 }),
            },
            &PRINCIPAL_ID,
            &neuron_store,
            TRANSACTION_FEES_E8S,
            NOW_SECONDS,
        )
        .unwrap_err();
        assert_matches!(error, MergeNeuronsError::SourceNeuronNotHotKeyOrController);

        // Target not authorized.
        let error = calculate_merge_neurons_effect(
            &NeuronId { id: 1 },
            &Merge {
                source_neuron_id: Some(NeuronId { id: 2 }),
            },
            &PRINCIPAL_ID,
            &neuron_store,
            TRANSACTION_FEES_E8S,
            NOW_SECONDS,
        )
        .unwrap_err();
        assert_matches!(error, MergeNeuronsError::TargetNeuronNotHotKeyOrController);
    }
```

**File:** rs/nns/governance/src/governance.rs (L2442-2458)
```rust
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
```

**File:** rs/nns/governance/src/governance.rs (L2549-2565)
```rust
    fn simulate_merge_neurons(
        &self,
        id: &NeuronId,
        caller: &PrincipalId,
        merge: manage_neuron::Merge,
    ) -> Result<ManageNeuronResponse, GovernanceError> {
        let now = self.env.now();

        // Step 1: calculates the effect of the merge.
        let effect = calculate_merge_neurons_effect(
            id,
            &merge,
            caller,
            &self.neuron_store,
            self.transaction_fee(),
            now,
        )?;
```

**File:** rs/nns/governance/src/neuron/types.rs (L239-245)
```rust
    /// Returns true if and only if `principal` is authorized to
    /// perform non-privileged operations, like vote and follow,
    /// on behalf of this neuron, i.e., if `principal` is either the
    /// controller or one of the authorized hot keys.
    pub(crate) fn is_authorized_to_vote(&self, principal: &PrincipalId) -> bool {
        self.is_hotkey_or_controller(principal)
    }
```

**File:** rs/nns/governance/src/neuron/types.rs (L779-791)
```rust
            // The controller and hotkeys are allowed to change Neuron Fund membership.
            JoinCommunityFund(_) | LeaveCommunityFund(_) => {
                if self.is_hotkey_or_controller(caller) {
                    Ok(())
                } else {
                    Err(GovernanceError::new_with_message(
                        ErrorType::NotAuthorized,
                        format!(
                            "Caller '{caller:?}' must be the controller or hotkey of the neuron to join or leave the neuron fund.",
                        ),
                    ))
                }
            }
```
