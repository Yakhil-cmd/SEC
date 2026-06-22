### Title
SNS `ManageNervousSystemParameters` Allows Immediate Increase of `neuron_minimum_dissolve_delay_to_vote_seconds` Without Any Delay Period, Disenfranchising Locked Neuron Holders - (File: `rs/sns/governance/src/governance.rs`)

---

### Summary

The SNS governance system allows the `neuron_minimum_dissolve_delay_to_vote_seconds` parameter to be changed immediately upon proposal execution, with no lockup or delay period. An SNS founding team that holds a majority of initial token distribution can attract users to stake with short dissolve delays, then immediately raise the minimum dissolve delay to the protocol maximum — locking users' tokens while simultaneously stripping them of all voting power. This is a direct analog to the validator commission manipulation: a trusted initial actor sets favorable terms, users lock in, then the actor unilaterally changes the terms with no recourse.

---

### Finding Description

The `perform_manage_nervous_system_parameters` function applies new `NervousSystemParameters` immediately upon proposal execution with no delay:

```rust
// rs/sns/governance/src/governance.rs
fn perform_manage_nervous_system_parameters(
    &mut self,
    proposed_params: NervousSystemParameters,
) -> Result<(), GovernanceError> {
    let new_params = proposed_params.inherit_from(self.nervous_system_parameters_or_panic());
    match new_params.validate() {
        Ok(()) => {
            self.proto.parameters = Some(new_params); // immediate, no delay
            Ok(())
        }
        ...
    }
}
```

The validation for `neuron_minimum_dissolve_delay_to_vote_seconds` only checks that the new value does not exceed `max_dissolve_delay_seconds`. There is no restriction on the magnitude of the increase, no rate-limiting, and no lockup period:

```rust
// rs/sns/governance/src/types.rs
fn validate_neuron_minimum_dissolve_delay_to_vote_seconds(&self) -> Result<(), String> {
    ...
    if neuron_minimum_dissolve_delay_to_vote_seconds > max_dissolve_delay_seconds {
        Err(...)
    } else {
        Ok(()) // any increase up to max is accepted immediately
    }
}
```

The `NervousSystemParameters` struct exposes `neuron_minimum_dissolve_delay_to_vote_seconds` as a directly settable field with no change-rate guard:

```rust
// rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto
optional uint64 neuron_minimum_dissolve_delay_to_vote_seconds = 8;
```

At proposal execution time, `compute_ballots_for_new_proposal` uses the live value of `neuron_minimum_dissolve_delay_to_vote_seconds` to determine eligibility, meaning the change takes effect for all future proposals immediately:

```rust
// rs/sns/governance/src/governance.rs
let min_dissolve_delay_for_vote = nervous_system_parameters
    .neuron_minimum_dissolve_delay_to_vote_seconds
    .expect("...");
...
if v.dissolve_delay_seconds(now_seconds) < min_dissolve_delay_for_vote {
    continue; // neuron excluded from voting
}
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

---

### Impact Explanation

An SNS founding team that holds an initial token majority (common in early SNS deployments per the decentralization swap model) can:

1. Launch the SNS with a low `neuron_minimum_dissolve_delay_to_vote_seconds` (e.g., 1 month) to attract broad participation.
2. Users stake tokens with 1-month dissolve delays, locking their tokens.
3. The founding team passes a `ManageNervousSystemParameters` proposal to set `neuron_minimum_dissolve_delay_to_vote_seconds` to the protocol maximum (e.g., 8 years = `max_dissolve_delay_seconds`).
4. All users with dissolve delays below 8 years are immediately stripped of voting power.
5. Users' tokens remain locked for their dissolve delay period — they cannot exit.
6. The founding team retains a monopoly on governance with no opposition possible.

This is a governance authorization bug with ledger conservation implications: users' tokens are effectively confiscated from a governance perspective while remaining physically locked. [5](#0-4) [6](#0-5) 

---

### Likelihood Explanation

**Medium.** SNS founding teams routinely hold a majority of initial token distribution during the decentralization swap phase. The `ManageNervousSystemParameters` proposal type is a standard, documented SNS action. No special exploit code is required — only a standard governance proposal. The attack is economically motivated (governance monopoly) and requires no technical sophistication beyond submitting a proposal. The integration test at `rs/sns/integration_tests/src/proposals.rs` confirms this proposal type executes immediately and takes effect on all subsequent proposals. [7](#0-6) 

---

### Recommendation

Implement a mandatory delay period before changes to `neuron_minimum_dissolve_delay_to_vote_seconds` take effect, analogous to the lockup period recommended in the external report. Specifically:

- Changes to `neuron_minimum_dissolve_delay_to_vote_seconds` should only apply to neurons created **after** the change, not retroactively to existing locked neurons.
- Alternatively, enforce that the new value cannot exceed the old value by more than a bounded increment per proposal, or require a time-delayed execution (e.g., the change takes effect only after the current minimum dissolve delay has elapsed).
- Add a validation check in `validate_neuron_minimum_dissolve_delay_to_vote_seconds` that compares the proposed value against the current live value and rejects increases beyond a defined threshold. [2](#0-1) [1](#0-0) 

---

### Proof of Concept

**Setup:**
- SNS launched with `neuron_minimum_dissolve_delay_to_vote_seconds = 2_592_000` (1 month).
- Founding team holds 60% of SNS tokens in neurons with 8-year dissolve delays.
- 1,000 community users stake with 1-month dissolve delays, attracted by the low barrier.

**Attack:**
```
ManageNervousSystemParameters {
    neuron_minimum_dissolve_delay_to_vote_seconds: Some(252_460_800), // 8 years
    ..Default::default()
}
```

**Result:**
- Proposal passes (founding team holds 60% voting power).
- `perform_manage_nervous_system_parameters` sets `self.proto.parameters` immediately.
- All 1,000 community neurons now have `dissolve_delay_seconds(now) < min_dissolve_delay_for_vote`.
- They are excluded from all future ballots via `compute_ballots_for_new_proposal`.
- Their tokens remain locked for 1 month — they cannot exit immediately.
- Founding team has 100% of effective voting power for the next month, and can pass any proposal (treasury drain, parameter changes, etc.) unopposed. [8](#0-7) [9](#0-8)

### Citations

**File:** rs/sns/governance/src/governance.rs (L2579-2617)
```rust
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
```

**File:** rs/sns/governance/src/governance.rs (L5225-5294)
```rust
    /// Computes the total potential voting power of the governance canister and ballots.
    fn compute_ballots_for_new_proposal(&self) -> Result<(u64, BTreeMap<String, Ballot>), String> {
        let now_seconds = self.env.now();

        let nervous_system_parameters = self.nervous_system_parameters_or_panic();

        // Voting power bonus parameters.
        let max_dissolve_delay = nervous_system_parameters
            .max_dissolve_delay_seconds
            .expect("NervousSystemParameters must have max_dissolve_delay_seconds");

        let max_age_bonus = nervous_system_parameters
            .max_neuron_age_for_age_bonus
            .expect("NervousSystemParameters must have max_neuron_age_for_age_bonus");

        let max_dissolve_delay_bonus_percentage = nervous_system_parameters
            .max_dissolve_delay_bonus_percentage
            .expect("NervousSystemParameters must have max_dissolve_delay_bonus_percentage");

        let max_age_bonus_percentage = nervous_system_parameters
            .max_age_bonus_percentage
            .expect("NervousSystemParameters must have max_age_bonus_percentage");

        let min_dissolve_delay_for_vote = nervous_system_parameters
            .neuron_minimum_dissolve_delay_to_vote_seconds
            .expect("NervousSystemParameters must have min_dissolve_delay_for_vote");

        let mut electoral_roll = BTreeMap::<String, Ballot>::new();
        let mut total_power: u128 = 0;

        for (k, v) in self.proto.neurons.iter() {
            // If this neuron is eligible to vote, record its
            // voting power at the time of proposal creation (now).
            if v.dissolve_delay_seconds(now_seconds) < min_dissolve_delay_for_vote {
                // Not eligible due to dissolve delay.
                continue;
            }

            let voting_power = v.voting_power(
                now_seconds,
                max_dissolve_delay,
                max_age_bonus,
                max_dissolve_delay_bonus_percentage,
                max_age_bonus_percentage,
            );

            total_power += voting_power as u128;
            electoral_roll.insert(
                k.clone(),
                Ballot {
                    vote: Vote::Unspecified as i32,
                    voting_power,
                    cast_timestamp_seconds: 0,
                },
            );
        }

        if total_power >= (u64::MAX as u128) {
            // The way the neurons are configured, the total voting
            // power on this proposal would overflow a u64!
            return Err("Voting power overflow.".to_string());
        }
        if electoral_roll.is_empty() {
            // Cannot make a proposal with no eligible voters.  This
            // is a precaution that shouldn't happen as we check that
            // the voter is allowed to vote.
            return Err("No eligible voters.".to_string());
        }

        Ok((total_power as u64, electoral_roll))
```

**File:** rs/sns/governance/src/types.rs (L752-772)
```rust
    /// Validates that the nervous system parameter
    /// neuron_minimum_dissolve_delay_to_vote_seconds is well-formed.
    fn validate_neuron_minimum_dissolve_delay_to_vote_seconds(&self) -> Result<(), String> {
        let max_dissolve_delay_seconds = self.validate_max_dissolve_delay_seconds()?;

        let neuron_minimum_dissolve_delay_to_vote_seconds = self
            .neuron_minimum_dissolve_delay_to_vote_seconds
            .ok_or_else(|| {
                "NervousSystemParameters.neuron_minimum_dissolve_delay_to_vote_seconds must be set"
                    .to_string()
            })?;

        if neuron_minimum_dissolve_delay_to_vote_seconds > max_dissolve_delay_seconds {
            Err(format!(
                "The minimum dissolve delay to vote ({neuron_minimum_dissolve_delay_to_vote_seconds}) cannot be greater than the max \
                dissolve delay ({max_dissolve_delay_seconds})"
            ))
        } else {
            Ok(())
        }
    }
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L1182-1185)
```text
  // The minimum dissolve delay a neuron must have to be eligible to vote.
  //
  // The chosen value must be smaller than max_dissolve_delay_seconds.
  optional uint64 neuron_minimum_dissolve_delay_to_vote_seconds = 8;
```

**File:** rs/sns/integration_tests/src/proposals.rs (L133-222)
```rust
/// Assert that ManageNervousSystemParameters proposals can be submitted, voted on, and executed
#[test]
fn test_manage_nervous_system_parameters_proposal_execution() {
    state_machine_test_on_sns_subnet(|runtime| {
        async move {
            // Initialize the ledger with an account for a user.
            let user = Sender::from_keypair(&TEST_USER1_KEYPAIR);
            let alloc = Tokens::from_tokens(1000).unwrap();

            let sys_params = NervousSystemParameters {
                neuron_claimer_permissions: Some(NeuronPermissionList {
                    permissions: NeuronPermissionType::all(),
                }),
                ..NervousSystemParameters::with_default_values()
            };

            let sns_init_payload = SnsTestsInitPayloadBuilder::new()
                .with_ledger_account(user.get_principal_id().0.into(), alloc)
                .with_nervous_system_parameters(sys_params)
                .build();

            let sns_canisters = SnsCanisters::set_up(&runtime, sns_init_payload).await;

            let neuron_id = sns_canisters
                .stake_and_claim_neuron(&user, Some(ONE_YEAR_SECONDS as u32))
                .await;

            let subaccount = neuron_id
                .subaccount()
                .expect("Error creating the subaccount");

            // Assert that invalid params are rejected on proposal submission
            let proposal_payload = Proposal {
                title: "Test invalid ManageNervousSystemParameters proposal".into(),
                action: Some(Action::ManageNervousSystemParameters(
                    NervousSystemParameters {
                        max_number_of_neurons: Some(
                            NervousSystemParameters::MAX_NUMBER_OF_NEURONS_CEILING + 1,
                        ),
                        ..Default::default()
                    },
                )),
                ..Default::default()
            };

            let error = sns_canisters
                .make_proposal(&user, &subaccount, proposal_payload)
                .await
                .unwrap_err();

            assert_eq!(error.error_type, ErrorType::InvalidProposal as i32);

            // Assert that valid params cause Governance system parameters to be updated
            let proposal_payload = Proposal {
                title: "Test valid ManageNervousSystemParameters proposal".into(),
                action: Some(Action::ManageNervousSystemParameters(
                    NervousSystemParameters {
                        transaction_fee_e8s: Some(120_001),
                        neuron_minimum_stake_e8s: Some(398_002_900),
                        ..Default::default()
                    },
                )),
                ..Default::default()
            };

            // Submit a proposal. It should then be executed because the submitter
            // has a majority stake and submitting also votes automatically.
            let proposal_id = sns_canisters
                .make_proposal(&user, &subaccount, proposal_payload)
                .await
                .unwrap();

            let proposal = sns_canisters.get_proposal(proposal_id).await;

            assert_eq!(proposal.action, 2);
            assert_ne!(proposal.decided_timestamp_seconds, 0);
            assert_ne!(proposal.executed_timestamp_seconds, 0);

            let live_sys_params: NervousSystemParameters = sns_canisters
                .governance
                .query_("get_nervous_system_parameters", candid_one, ())
                .await?;

            assert_eq!(live_sys_params.transaction_fee_e8s, Some(120_001));
            assert_eq!(live_sys_params.neuron_minimum_stake_e8s, Some(398_002_900));

            Ok(())
        }
    })
}
```

**File:** rs/sns/governance/src/gen/ic_sns_governance.pb.v1.rs (L1630-1657)
```rust
/// The nervous system's parameters, which are parameters that can be changed, via proposals,
/// by each nervous system community.
/// For some of the values there are specified minimum values (floor) or maximum values
/// (ceiling). The motivation for this is a) to prevent that the nervous system accidentally
/// chooses parameters that result in an non-upgradable (and thus stuck) governance canister
/// and b) to prevent the canister from growing too big (which could harm the other canisters
/// on the subnet).
///
/// Required invariant: the canister code assumes that all system parameters are always set.
#[derive(
    candid::CandidType,
    candid::Deserialize,
    comparable::Comparable,
    Clone,
    PartialEq,
    ::prost::Message,
)]
pub struct NervousSystemParameters {
    /// The number of e8s (10e-8 of a token) that a rejected
    /// proposal costs the proposer.
    #[prost(uint64, optional, tag = "1")]
    pub reject_cost_e8s: ::core::option::Option<u64>,
    /// The minimum number of e8s (10e-8 of a token) that can be staked in a neuron.
    ///
    /// To ensure that staking and disbursing of the neuron work, the chosen value
    /// must be larger than the transaction_fee_e8s.
    #[prost(uint64, optional, tag = "2")]
    pub neuron_minimum_stake_e8s: ::core::option::Option<u64>,
```
