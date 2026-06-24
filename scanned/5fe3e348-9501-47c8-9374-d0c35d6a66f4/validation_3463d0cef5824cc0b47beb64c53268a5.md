### Title
SNS `UpgradeSnsControlledCanister` Proposals Classified as Normal Criticality with a 1-Day Minimum Voting Period Floor, Enabling Rapid Malicious Dapp Upgrades - (File: rs/sns/governance/src/types.rs)

### Summary
The SNS governance canister enforces a minimum voting period floor of only 1 day (86,400 seconds) for all proposals. `UpgradeSnsControlledCanister` is classified as `ProposalCriticality::Normal` — not Critical — so it does not receive the 5-day minimum floor that Critical proposals get. An SNS configured at the minimum `initial_voting_period_seconds = ONE_DAY_SECONDS` can have dapp canister upgrades adopted in 24 hours, giving token holders insufficient time to notice, evaluate, and exit before a malicious upgrade executes.

### Finding Description
**Root cause 1 — floor too low:**

`INITIAL_VOTING_PERIOD_SECONDS_FLOOR` is set to `ONE_DAY_SECONDS` (86,400 s):

```rust
pub const INITIAL_VOTING_PERIOD_SECONDS_FLOOR: u64 = ONE_DAY_SECONDS;
``` [1](#0-0) 

The validation function `validate_initial_voting_period_seconds` enforces this floor but nothing higher: [2](#0-1) 

**Root cause 2 — `UpgradeSnsControlledCanister` is `Normal`, not `Critical`:**

In `topic_descriptions()`, the `DappCanisterManagement` topic (which owns `UpgradeSnsControlledCanister`) is declared `is_critical: false`:

```rust
TopicInfo::<NativeFunctions> {
    topic: Topic::DappCanisterManagement,
    ...
    functions: NativeFunctions {
        native_functions: vec![
            NativeAction::UpgradeSnsControlledCanister as u64,
            ...
        ],
    },
    is_critical: false,   // ← Normal, not Critical
},
``` [3](#0-2) 

This is confirmed by the proposal-topics test: [4](#0-3) 

**Consequence — Normal proposals use the raw `initial_voting_period_seconds`:**

`voting_duration_parameters` only applies the 5-day hard floor for `ProposalCriticality::Critical`. For `Normal`, it passes through whatever `initial_voting_period_seconds` is configured:

```rust
ProposalCriticality::Normal => VotingDurationParameters {
    initial_voting_period: PbDuration {
        seconds: initial_voting_period_seconds,   // ← no floor applied
    },
    ...
},
ProposalCriticality::Critical => {
    ...
    seconds: Some(initial_voting_period_seconds.max(5 * ONE_DAY_SECONDS)),  // ← 5-day floor
    ...
}
``` [5](#0-4) 

**Consequence — Normal proposals need only 3% of total voting power:**

`ProposalCriticality::Normal` requires only 3% of total voting power and 50% of exercised voting power, versus 20% / 67% for Critical: [6](#0-5) 

**Attack chain:**

1. An SNS is deployed with `initial_voting_period_seconds = ONE_DAY_SECONDS` (the minimum allowed). This is a valid configuration accepted by `validate_initial_voting_period_seconds`.
2. The SNS developer team, which commonly holds majority voting power in early-stage SNS deployments (developer neurons issued at genesis), submits a malicious `UpgradeSnsControlledCanister` proposal targeting a dapp canister that holds user funds.
3. Because the proposal is `Normal` criticality, it only needs 3% of total voting power to adopt and runs for just 24 hours.
4. The proposal is adopted and `perform_upgrade_sns_controlled_canister` executes, installing the malicious WASM on the dapp canister: [7](#0-6) 
5. Token holders had only 24 hours to notice the proposal, audit the WASM, dissolve neurons, and exit — which is insufficient.

By contrast, `ManageNervousSystemParameters` (which could be used to reduce the voting period in the first place) is `Critical` under `DaoCommunitySettings`: [8](#0-7) 

So the governance parameter change itself requires 5 days + 20% total voting power, but the subsequent upgrade proposal that actually drains funds only requires 1 day + 3% total voting power.

### Impact Explanation
A developer team with majority voting power in an early-stage SNS can push through a malicious `UpgradeSnsControlledCanister` proposal in as little as 24 hours. The malicious WASM can drain all user funds held in the dapp canister, redirect token flows, or install a backdoor. Token holders have no meaningful window to react: 24 hours is insufficient to notice the proposal, decompile and audit an arbitrary WASM, dissolve neurons (which have a minimum dissolve delay), and exit positions.

### Likelihood Explanation
Early-stage SNS deployments routinely have developer neurons that collectively hold majority voting power immediately after the decentralization swap. The minimum floor of 1 day is a protocol-enforced constant that any SNS can legitimately configure. The combination of a short floor and Normal criticality for upgrade proposals creates a structural window that is directly analogous to the 12-hour timelock in the original report.

### Recommendation
1. **Raise `INITIAL_VOTING_PERIOD_SECONDS_FLOOR`** to at least 2–4 days (172,800–345,600 seconds) so that even at the minimum, token holders have a meaningful reaction window.
2. **Reclassify `UpgradeSnsControlledCanister` as `ProposalCriticality::Critical`** (or at minimum add a per-action voting period floor for upgrade proposals), so that dapp canister upgrades receive the same 5-day minimum and 20%/67% voting power thresholds as treasury transfers and other high-impact actions.
3. Consider requiring a separate "timelock" phase between proposal adoption and execution for upgrade proposals, analogous to the recommendation in the original report.

### Proof of Concept

```
1. Deploy SNS with:
     initial_voting_period_seconds = 86400  (ONE_DAY_SECONDS, the minimum floor)
     wait_for_quiet_deadline_increase_seconds = 1  (the minimum floor)

2. Developer team (holding >50% voting power from genesis neurons) submits:
     Action::UpgradeSnsControlledCanister {
         canister_id: <dapp_canister_holding_user_funds>,
         new_canister_wasm: <malicious_wasm_that_drains_funds>,
         mode: Upgrade,
     }

3. Because UpgradeSnsControlledCanister is ProposalCriticality::Normal:
   - voting_duration_parameters() returns initial_voting_period = 86400 s (no 5-day floor applied)
   - minimum_yes_proportion_of_total = 3%  (not 20%)
   - minimum_yes_proportion_of_exercised = 50%  (not 67%)

4. Developer neurons vote Yes immediately. With >50% voting power and
   minimum_yes_proportion_of_exercised = 50%, the proposal is adopted
   within seconds via early-decision logic.

5. perform_upgrade_sns_controlled_canister() executes, installing the
   malicious WASM. All user funds in the dapp canister are drained.

6. Token holders had at most 24 hours (and in practice far less due to
   early-decision) to react — insufficient to audit the WASM and exit.
```

### Citations

**File:** rs/sns/governance/src/types.rs (L396-398)
```rust
    /// This is a lower bound for `initial_voting_period_seconds`. Exceeding it may cause
    /// degradation in the governance canister or the subnet hosting the SNS.
    pub const INITIAL_VOTING_PERIOD_SECONDS_FLOOR: u64 = ONE_DAY_SECONDS;
```

**File:** rs/sns/governance/src/types.rs (L653-673)
```rust
    /// Validates that the nervous system parameter initial_voting_period_seconds is well-formed.
    fn validate_initial_voting_period_seconds(&self) -> Result<(), String> {
        let initial_voting_period_seconds =
            self.initial_voting_period_seconds.ok_or_else(|| {
                "NervousSystemParameters.initial_voting_period_seconds must be set".to_string()
            })?;

        if initial_voting_period_seconds < Self::INITIAL_VOTING_PERIOD_SECONDS_FLOOR {
            Err(format!(
                "NervousSystemParameters.initial_voting_period_seconds must be greater than {}",
                Self::INITIAL_VOTING_PERIOD_SECONDS_FLOOR
            ))
        } else if initial_voting_period_seconds > Self::INITIAL_VOTING_PERIOD_SECONDS_CEILING {
            Err(format!(
                "NervousSystemParameters.initial_voting_period_seconds must be less than {}",
                Self::INITIAL_VOTING_PERIOD_SECONDS_CEILING
            ))
        } else {
            Ok(())
        }
    }
```

**File:** rs/sns/governance/src/types.rs (L1822-1850)
```rust
        match proposal_criticality {
            ProposalCriticality::Normal => VotingDurationParameters {
                initial_voting_period: PbDuration {
                    seconds: initial_voting_period_seconds,
                },
                wait_for_quiet_deadline_increase: PbDuration {
                    seconds: wait_for_quiet_deadline_increase_seconds,
                },
            },

            ProposalCriticality::Critical => {
                let initial_voting_period_seconds =
                    initial_voting_period_seconds.unwrap_or_default();
                let wait_for_quiet_deadline_increase_seconds =
                    wait_for_quiet_deadline_increase_seconds.unwrap_or_default();

                VotingDurationParameters {
                    initial_voting_period: PbDuration {
                        seconds: Some(initial_voting_period_seconds.max(5 * ONE_DAY_SECONDS)),
                    },
                    wait_for_quiet_deadline_increase: PbDuration {
                        seconds: Some(wait_for_quiet_deadline_increase_seconds.max(
                            2 * ONE_DAY_SECONDS + ONE_DAY_SECONDS / 2, // 2.5 days
                        )),
                    },
                }
            }
        }
    }
```

**File:** rs/sns/governance/src/topics.rs (L64-78)
```rust
    [
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

**File:** rs/sns/governance/src/topics.rs (L92-105)
```rust
        TopicInfo::<NativeFunctions> {
            topic: Topic::DappCanisterManagement,
            name: "Dapp canister management".to_string(),
            description: "Proposals to upgrade the registered dapp canisters and dapp upgrades via built-in or custom logic and updates to frontend assets.".to_string(),
            functions: NativeFunctions {
                native_functions: vec![
                    NativeAction::UpgradeSnsControlledCanister as u64,
                    NativeAction::RegisterDappCanisters as u64,
                    NativeAction::ManageDappCanisterSettings as u64,
                ],
            },
            extension_operations: vec![],
            is_critical: false,
        },
```

**File:** rs/sns/governance/src/governance/proposal_topics_tests.rs (L118-125)
```rust
        // DappCanisterManagement
        (
            pb::proposal::Action::UpgradeSnsControlledCanister(Default::default()),
            Ok((
                Some(pb::Topic::DappCanisterManagement),
                ProposalCriticality::Normal,
            )),
        ),
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

**File:** rs/sns/governance/src/governance.rs (L2644-2693)
```rust
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
```
