### Title
SNS Neurons' Fund Participants Permanently Lose Critical Neuron Management Capabilities Including Ability to Disable Auto-Stake Maturity, Dissolve, or Disburse - (`rs/sns/governance/src/types.rs`)

### Summary

When an NNS neuron joins the Neurons' Fund and an SNS swap commits, the resulting SNS neurons are created with NNS Governance as the sole `ManagePrincipals` holder. The NNS neuron's controller receives only `Vote`, `SubmitProposal`, and `ManageVotingPermission` — permanently losing the ability to configure dissolve state, disburse, split, merge/disburse/stake maturity, or disable `auto_stake_maturity`. There is no opt-out mechanism. This is a direct IC analog to the AutoCompounder vulnerability class: a user opts into a feature (Neurons' Fund), their asset is transferred to a controlling entity (NNS Governance), and critical management functions become permanently inaccessible.

### Finding Description

When an SNS swap finalizes with Neurons' Fund participation, `claim_swap_neurons` in SNS Governance is called by the Swap canister. For each Neurons' Fund participant, `construct_permissions` builds the neuron's permission list: [1](#0-0) 

The NNS neuron controller receives only `PERMISSIONS_FOR_NEURONS_FUND_NNS_NEURON_CONTROLLER`, which the integration test confirms is `{Vote, SubmitProposal, ManageVotingPermission}`: [2](#0-1) 

NNS Governance canister itself holds the full `neuron_claimer_permissions` set (all permissions including `ManagePrincipals`, `ConfigureDissolveState`, `Disburse`, `Split`, `MergeMaturity`, `DisburseMaturity`, `StakeMaturity`): [3](#0-2) 

Additionally, `auto_stake_maturity` is forcibly set to `true` for all Neurons' Fund SNS neurons at creation: [4](#0-3) 

This is set in `claim_swap_neurons`: [5](#0-4) 

The SNS `ChangeAutoStakeMaturity` operation is part of `Configure`, which requires `ConfigureDissolveState`: [6](#0-5) 

Since the NNS neuron controller lacks `ConfigureDissolveState`, they cannot disable `auto_stake_maturity`. They also cannot start dissolving (no `ConfigureDissolveState`), disburse (no `Disburse`), or split (no `Split`). Even if a neuron's dissolve delay reaches zero, the user cannot disburse it.

Furthermore, `refresh_neuron` in SNS Governance explicitly blocks NF-controlled neurons from being refreshed: [7](#0-6) 

There is no NNS Governance mechanism to perform SNS neuron operations on behalf of users, and no opt-out path exists. The `ManagePrincipals` permission is held exclusively by NNS Governance and cannot be transferred to the user.

### Impact Explanation

Any NNS neuron controller whose neuron participates in an SNS swap via the Neurons' Fund permanently loses the ability to:

1. **Disable `auto_stake_maturity`** — maturity is permanently auto-staked, compounding indefinitely with no user control.
2. **Start dissolving** — SNS tokens are locked with no user-initiated path to liquidity.
3. **Disburse** — even after a neuron's dissolve delay expires, the user cannot retrieve their SNS tokens.
4. **Split, merge, disburse, or stake maturity** — all token management operations are blocked.
5. **Add/remove principals** — the user cannot delegate management to another principal.

NNS Governance holds `ManagePrincipals` but exposes no canister method to perform SNS neuron operations on behalf of users. The SNS tokens from Neurons' Fund participation are effectively permanently locked under NNS Governance's control with no user-accessible recovery path.

### Likelihood Explanation

This affects every NNS neuron that joins the Neurons' Fund and whose maturity is used in a committed SNS swap — a routine, protocol-encouraged operation. The Neurons' Fund is a core NNS feature. The permission structure is applied unconditionally in `construct_permissions` for all `NeuronsFund` participant recipes. No special attacker action is required; the restriction is triggered by normal user participation.

### Recommendation

1. **Document** the permission restrictions clearly in the Neurons' Fund opt-in flow so users understand they will lose `ConfigureDissolveState`, `Disburse`, `Split`, and maturity management capabilities on resulting SNS neurons.
2. **Consider** adding an NNS Governance method that allows NF neuron controllers to request specific SNS neuron operations (e.g., start dissolving, disburse after dissolution) to be executed by NNS Governance on their behalf.
3. **Consider** whether `auto_stake_maturity = true` should be user-configurable via a dedicated NNS proposal type, or whether the restriction should be explicitly acknowledged in the Neurons' Fund join flow.

### Proof of Concept

1. NNS neuron controller calls `manage_neuron` with `JoinCommunityFund` on their NNS neuron.
2. An SNS swap is created via `CreateServiceNervousSystem` with Neurons' Fund participation enabled.
3. The swap commits; `claim_swap_neurons` is called by the Swap canister, creating SNS neurons with `auto_stake_maturity = Some(true)` and permissions `{NNS Governance: all, user: Vote+SubmitProposal+ManageVotingPermission}`.
4. The user attempts `manage_neuron` with `Configure { ChangeAutoStakeMaturity { false } }` on their SNS neuron — rejected with `NotAuthorized` because they lack `ConfigureDissolveState`.
5. The user attempts `manage_neuron` with `Configure { StartDissolving {} }` — rejected with `NotAuthorized`.
6. Even after the dissolve delay elapses, the user attempts `Disburse` — rejected with `NotAuthorized`.
7. No NNS Governance method exists to perform these operations on the user's behalf. The SNS tokens remain permanently locked. [8](#0-7) [5](#0-4) [9](#0-8)

### Citations

**File:** rs/sns/governance/src/types.rs (L2353-2406)
```rust
    pub(crate) fn construct_permissions(
        &self,
        neuron_claimer_permissions: NeuronPermissionList,
    ) -> Result<Vec<NeuronPermission>, String> {
        let mut permissions = vec![];

        let controller = self
            .controller
            .as_ref()
            .ok_or("Expected controller to be present in NeuronRecipe".to_string())?;

        permissions.push(NeuronPermission::new(
            controller,
            neuron_claimer_permissions.permissions,
        ));

        let Some(participant) = &self.participant else {
            return Err("Expected participant to be present in NeuronRecipe".to_string());
        };

        if let Participant::NeuronsFund(neurons_fund_participant) = participant {
            let nns_neuron_controller = neurons_fund_participant.nns_neuron_controller.ok_or(
                "Expected the nns_neuron_controller to be present for NeuronsFundParticipant"
                    .to_string(),
            )?;
            permissions.push(NeuronPermission::new(
                &nns_neuron_controller,
                Neuron::PERMISSIONS_FOR_NEURONS_FUND_NNS_NEURON_CONTROLLER
                    .iter()
                    .map(|p| *p as i32)
                    .collect(),
            ));

            for hotkey in neurons_fund_participant
                .nns_neuron_hotkeys
                .as_ref()
                .ok_or(
                    "Expected the nns_neuron_hotkeys to be present for NeuronsFundParticipant"
                        .to_string(),
                )?
                .principals
                .iter()
            {
                permissions.push(NeuronPermission::new(
                    hotkey,
                    Neuron::PERMISSIONS_FOR_NEURONS_FUND_NNS_NEURON_HOTKEY
                        .iter()
                        .map(|p| *p as i32)
                        .collect(),
                ));
            }
        }

        Ok(permissions)
```

**File:** rs/nervous_system/integration_tests/tests/sns_lifecycle.rs (L1412-1444)
```rust
                let neurons_fund_participant_neuron_permissions =
                    neurons_fund_neuron_controllers_to_neuron_portions
                        .values()
                        .flat_map(|neurons_fund_neuron_portion| {
                            // The controller of Neurons' Fund neurons is NNS Governance.
                            let controller = PrincipalId::from(GOVERNANCE_CANISTER_ID);
                            vec![
                                // Add governance as the controller
                                (controller, neuron_claimer_permissions.clone()),
                                // Add the controller of the NNS neuron as a hotkey that also has ManageVotingPermissions
                                (
                                    neurons_fund_neuron_portion.controller.unwrap(),
                                    BTreeSet::from([
                                        NeuronPermissionType::Vote,
                                        NeuronPermissionType::SubmitProposal,
                                        NeuronPermissionType::ManageVotingPermission,
                                    ]),
                                ),
                            ]
                            .into_iter()
                            .chain(
                                neurons_fund_neuron_portion.hotkeys.clone().into_iter().map(
                                    |hotkey| {
                                        (
                                            hotkey,
                                            BTreeSet::from([
                                                NeuronPermissionType::Vote,
                                                NeuronPermissionType::SubmitProposal,
                                            ]),
                                        )
                                    },
                                ),
                            )
```

**File:** rs/nervous_system/integration_tests/tests/sns_lifecycle.rs (L1573-1583)
```rust
                    // Validate `auto_stake_maturity`:
                    {
                        let expected_auto_stake_maturity = if is_neuron_from_direct_participation {
                            None
                        } else {
                            Some(true)
                        };
                        assert_eq!(
                            sns_neuron.auto_stake_maturity, expected_auto_stake_maturity,
                            "{sns_neuron:#?}"
                        );
```

**File:** rs/sns/governance/src/neuron.rs (L817-839)
```rust
    /// "NF neurons" are defined as neurons where the NNS governance canister
    /// has the the `ManagePrincipals` permission and is the only principal that
    /// does.
    pub fn is_neurons_fund_controlled(&self) -> bool {
        let principals_with_manage_principals_permission = self
            .permissions
            .iter()
            .filter_map(|p| {
                let manage_principals_present = p.permission_type.iter().any(|permission| {
                    NeuronPermissionType::try_from(*permission).ok()
                        == Some(NeuronPermissionType::ManagePrincipals)
                });
                if manage_principals_present {
                    p.principal
                } else {
                    None
                }
            })
            .collect::<Vec<_>>();

        principals_with_manage_principals_permission
            == vec![PrincipalId::from(ic_nns_constants::GOVERNANCE_CANISTER_ID)]
    }
```

**File:** rs/sns/governance/src/governance.rs (L4242-4252)
```rust
        // First ensure that the neuron was not created via an NNS Neurons' Fund participation in the
        // decentralization swap
        {
            let neuron = self.get_neuron_result(nid)?;

            if neuron.is_neurons_fund_controlled() {
                return Err(GovernanceError::new_with_message(
                    ErrorType::PreconditionFailed,
                    "Cannot refresh an SNS Neuron controlled by the Neurons' Fund",
                ));
            }
```

**File:** rs/sns/governance/src/governance.rs (L4507-4529)
```rust
            let neuron = Neuron {
                id: Some(neuron_id.clone()),
                permissions: neuron_recipe
                    .construct_permissions_or_panic(neuron_claimer_permissions.clone()),
                cached_neuron_stake_e8s: neuron_recipe.get_stake_e8s_or_panic(),
                neuron_fees_e8s: 0,
                created_timestamp_seconds: now,
                aging_since_timestamp_seconds: now,
                topic_followees: Some(neuron_recipe.construct_topic_followees()),
                maturity_e8s_equivalent: 0,
                dissolve_state: Some(DissolveState::DissolveDelaySeconds(
                    neuron_recipe.get_dissolve_delay_seconds_or_panic(),
                )),
                voting_power_percentage_multiplier: DEFAULT_VOTING_POWER_PERCENTAGE_MULTIPLIER,
                source_nns_neuron_id: neuron_recipe.source_nns_neuron_id(),
                staked_maturity_e8s_equivalent: None,
                auto_stake_maturity: neuron_recipe.construct_auto_staking_maturity(),
                vesting_period_seconds: None,
                disburse_maturity_in_progress: vec![],

                // Deprecated
                followees: btreemap! {},
            };
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L1837-1855)
```text
  // Changes auto-stake maturity for this Neuron. While on, auto-stake
  // maturity will cause all the maturity generated by voting rewards
  // to this neuron to be automatically staked and contribute to the
  // voting power of the neuron.
  message ChangeAutoStakeMaturity {
    bool requested_setting_for_auto_stake_maturity = 1;
  }

  // Commands that only configure a given neuron, but do not interact
  // with the outside world. They all require the caller to have
  // `NeuronPermissionType::ConfigureDissolveState` for the neuron.
  message Configure {
    oneof operation {
      IncreaseDissolveDelay increase_dissolve_delay = 1;
      StartDissolving start_dissolving = 2;
      StopDissolving stop_dissolving = 3;
      SetDissolveTimestamp set_dissolve_timestamp = 4;
      ChangeAutoStakeMaturity change_auto_stake_maturity = 5;
    }
```
