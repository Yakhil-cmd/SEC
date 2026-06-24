### Title
SNS `SnsFrameworkManagement` Topic Classified as Non-Critical Despite Enabling Core Canister Upgrades - (`File: rs/sns/governance/src/topics.rs`)

### Summary

The SNS governance system classifies `UpgradeSnsToNextVersion` (id=7) and `AdvanceSnsTargetVersion` (id=15) under the `SnsFrameworkManagement` topic with `is_critical: false`. These proposals can upgrade core SNS canisters — including the governance canister itself, root, ledger, swap, archive, and index — yet they only require the normal voting threshold (50% of exercised voting power, 3% of total), not the critical threshold (67% of exercised, 20% of total). An attacker holding 51–66% of an SNS's voting power can force core canister upgrades that the SNS community intended to require a supermajority, violating the invariant that high-impact governance actions must clear a higher quorum bar.

### Finding Description

In `rs/sns/governance/src/topics.rs`, the `topic_descriptions()` function statically assigns every native proposal type to a topic and a criticality level: [1](#0-0) 

`SnsFrameworkManagement` is explicitly marked `is_critical: false`, placing `UpgradeSnsToNextVersion` and `AdvanceSnsTargetVersion` in the normal-criticality bucket. The criticality is then read by `get_topic_and_criticality_for_action`: [2](#0-1) 

And the resulting `ProposalCriticality` drives the voting-power thresholds stamped onto every new proposal at submission time: [3](#0-2) 

Normal proposals need only 50% of exercised voting power and 3% of total; critical proposals need 67% and 20% respectively. [4](#0-3) 

`UpgradeSnsToNextVersion` executes `upgrade_canister_directly` on the root canister or `upgrade_non_root_canister` on governance, ledger, swap, archive, and index — the entire SNS framework: [5](#0-4) 

`AdvanceSnsTargetVersion` sets `proto.target_version`, which then drives automatic periodic upgrades of the same core canisters: [6](#0-5) 

The contrast with correctly-protected actions is clear: `ManageNervousSystemParameters` (which can change `custom_proposal_criticality`, voting periods, and quorum floors) is under `DaoCommunitySettings` with `is_critical: true`: [7](#0-6) 

### Impact Explanation

An SNS token holder who accumulates 51–66% of voting power can submit and pass an `UpgradeSnsToNextVersion` or `AdvanceSnsTargetVersion` proposal under the normal threshold (>50% of exercised), even though the SNS community's intent — expressed through the critical-proposal mechanism — is that actions affecting the core governance infrastructure require a supermajority (>67%). This allows the attacker to:

1. Force an upgrade of the SNS governance canister to the latest NNS-blessed version before the SNS community has reviewed it.
2. Set `target_version` to a future NNS-blessed version, triggering automatic periodic upgrades of all core SNS canisters without further community approval.

The impact is partially mitigated because upgrades are constrained to NNS-blessed WASMs — arbitrary code cannot be injected. However, the attacker can force adoption of a version the SNS community has not reviewed, and if that version contains a regression or a vulnerability, the SNS is exposed. This directly violates the invariant that high-impact actions on the governance framework must clear a higher quorum bar.

### Likelihood Explanation

Any SNS participant who accumulates a simple majority (51%) of voting power — through token acquisition, delegation, or neuron following — can exploit this. The attack requires no privileged access, no key compromise, and no subnet-level corruption. It is reachable via a standard `manage_neuron` → `MakeProposal` ingress call to the SNS governance canister. The attacker needs only enough voting power to pass a normal proposal, not a critical one.

### Recommendation

Mark the `SnsFrameworkManagement` topic as critical (`is_critical: true`) in `topic_descriptions()`:

```rust
TopicInfo::<NativeFunctions> {
    topic: Topic::SnsFrameworkManagement,
    ...
    is_critical: true,  // was false
},
```

This ensures `UpgradeSnsToNextVersion` and `AdvanceSnsTargetVersion` require 67% of exercised voting power and 20% of total voting power, consistent with the protection already applied to `ManageNervousSystemParameters` and treasury actions. Alternatively, if the design intent is to keep SNS framework upgrades easy to pass, the documentation and the `custom_proposal_criticality` mechanism should explicitly acknowledge this trade-off and allow individual SNS communities to opt in to critical-level protection for these actions.

### Proof of Concept

1. Deploy an SNS where neuron A holds 55% of voting power and neuron B holds 45%.
2. Submit `Action::UpgradeSnsToNextVersion({})` from neuron A.
3. The proposal is stamped with `minimum_yes_proportion_of_exercised = 50%` (normal threshold) because `SnsFrameworkManagement` has `is_critical: false`.
4. Neuron A's 55% > 50% of exercised → proposal passes immediately via early decision.
5. The SNS governance canister is upgraded to the next NNS-blessed version without neuron B's consent.
6. Repeat with `Action::AdvanceSnsTargetVersion` to set a target version that triggers automatic upgrades of all core SNS canisters in subsequent heartbeats.

Had `SnsFrameworkManagement` been critical, step 4 would require 67% of exercised voting power, which neuron A's 55% does not satisfy — the proposal would remain open until the voting period expires and then be rejected. [1](#0-0) [8](#0-7) [6](#0-5) [3](#0-2)

### Citations

**File:** rs/sns/governance/src/topics.rs (L65-78)
```rust
        TopicInfo::<NativeFunctions> {
            topic: Topic::DaoCommunitySettings,
            name: "DAO community settings".to_string(),
            description: "Proposals to set the direction of the DAO by tokenomics & branding, such as the name and description, token name etc".to_string(),
            functions: NativeFunctions {
                native_functions: vec![
                    NativeAction::ManageNervousSystemParameters as u64,
                    NativeAction::ManageLedgerParameters as u64,
                    NativeAction::ManageSnsMetadata as u64,
                ],
            },
            extension_operations: vec![],
            is_critical: true,
        },
```

**File:** rs/sns/governance/src/topics.rs (L79-91)
```rust
        TopicInfo::<NativeFunctions> {
            topic: Topic::SnsFrameworkManagement,
            name: "SNS framework management".to_string(),
            description: "Proposals to upgrade and manage the SNS DAO framework.".to_string(),
            functions: NativeFunctions {
                native_functions: vec![
                    NativeAction::UpgradeSnsToNextVersion as u64,
                    NativeAction::AdvanceSnsTargetVersion as u64,
                ],
            },
            extension_operations: vec![],
            is_critical: false,
        },
```

**File:** rs/sns/governance/src/topics.rs (L304-331)
```rust
    pub fn get_topic_and_criticality_for_action(
        &self,
        action: &pb::proposal::Action,
    ) -> Result<(Option<pb::Topic>, ProposalCriticality), String> {
        let maybe_topic = self.get_topic_for_action(action)?;

        let is_critical_by_customization = self
            .proto
            .parameters
            .as_ref()
            .and_then(|p| p.custom_proposal_criticality.as_ref())
            .map(|c| {
                c.additional_critical_native_action_ids
                    .contains(&u64::from(action))
            })
            .unwrap_or(false);

        let criticality = if is_critical_by_customization {
            ProposalCriticality::Critical
        } else {
            // Fall back to default proposal criticality (if a topic isn't defined).
            maybe_topic
                .map(|topic| topic.proposal_criticality())
                .unwrap_or(ProposalCriticality::default())
        };

        Ok((maybe_topic, criticality))
    }
```

**File:** rs/sns/governance/proposal_criticality/src/lib.rs (L17-38)
```rust
impl ProposalCriticality {
    pub fn voting_power_thresholds(self) -> VotingPowerThresholds {
        match self {
            Self::Normal => VotingPowerThresholds {
                minimum_yes_proportion_of_total: Percentage {
                    basis_points: Some(300), // 3%
                },
                minimum_yes_proportion_of_exercised: Percentage {
                    basis_points: Some(5000), // 50%
                },
            },

            Self::Critical => VotingPowerThresholds {
                minimum_yes_proportion_of_total: Percentage {
                    basis_points: Some(2000), // 20%
                },
                minimum_yes_proportion_of_exercised: Percentage {
                    basis_points: Some(6700), // 67%
                },
            },
        }
    }
```

**File:** rs/sns/governance/src/types.rs (L451-467)
```rust
    pub const DEFAULT_MINIMUM_YES_PROPORTION_OF_TOTAL_VOTING_POWER: Percentage =
        Percentage::from_basis_points(300); // 3%

    /// Same as DEFAULT_MINIMUM_YES_PROPORTION_OF_TOTAL_VOTING_POWER, but for "critical" proposals
    pub const CRITICAL_MINIMUM_YES_PROPORTION_OF_TOTAL_VOTING_POWER: Percentage =
        Percentage::from_basis_points(2_000); // 20%

    /// The proportion of "yes votes" as basis points of the exercised voting power
    /// that is required for the proposal to be adopted. For example, if this field
    /// is 5000bp, then the proposal can only be adopted if the number of "yes
    /// votes" is greater than or equal to 50% of the exercised voting power.
    pub const DEFAULT_MINIMUM_YES_PROPORTION_OF_EXERCISED_VOTING_POWER: Percentage =
        Percentage::from_basis_points(5_000); // 50%

    /// Same as DEFAULT_MINIMUM_YES_PROPORTION_OF_EXERCISED_VOTING_POWER, but for "critical" proposals
    pub const CRITICAL_MINIMUM_YES_PROPORTION_OF_EXERCISED_VOTING_POWER: Percentage =
        Percentage::from_basis_points(6_700); // 67%
```

**File:** rs/sns/governance/src/governance.rs (L2822-2908)
```rust
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
```

**File:** rs/sns/governance/src/governance.rs (L3257-3277)
```rust
    fn perform_advance_target_version(
        &mut self,
        new_target: Version,
    ) -> Result<(), GovernanceError> {
        let (_, target_version) = self
            .proto
            .validate_new_target_version(Some(new_target))
            .map_err(|err: String| {
                GovernanceError::new_with_message(ErrorType::InvalidProposal, err)
            })?;

        self.push_to_upgrade_journal(upgrade_journal_entry::TargetVersionSet::new(
            self.proto.target_version.clone(),
            target_version.clone(),
            false,
        ));

        self.proto.target_version = Some(target_version);

        Ok(())
    }
```
