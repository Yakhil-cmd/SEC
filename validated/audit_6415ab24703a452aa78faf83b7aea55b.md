### Title
Hardcoded `MATURITY_DISBURSEMENT_DELAY_SECONDS` Cannot Be Updated via SNS Governance Proposals — (`rs/sns/governance/src/governance.rs`)

---

### Summary

In SNS Governance, the maturity disbursement delay is hardcoded as a compile-time constant (`MATURITY_DISBURSEMENT_DELAY_SECONDS = 7 * 24 * 3600`) and is not a field in `NervousSystemParameters`. As a result, no `ManageNervousSystemParameters` proposal can ever update it. SNS communities are permanently locked into a 7-day disbursement delay with no governance-level recourse short of a full canister upgrade. The same pattern exists in NNS Governance (`DISBURSEMENT_DELAY_SECONDS`), where `ManageNetworkEconomics` proposals cannot update it either.

---

### Finding Description

In `rs/sns/governance/src/governance.rs`, the maturity disbursement delay is declared as a module-level constant:

```rust
pub const MATURITY_DISBURSEMENT_DELAY_SECONDS: u64 = 7 * 24 * 3600;
``` [1](#0-0) 

This constant is consumed directly in `disburse_maturity()` to compute the finalization timestamp:

```rust
finalize_disbursement_timestamp_seconds: Some(
    now_seconds + MATURITY_DISBURSEMENT_DELAY_SECONDS,
),
``` [2](#0-1) 

The `NervousSystemParameters` proto message — the only struct that `ManageNervousSystemParameters` proposals can modify — contains no `maturity_disbursement_delay_seconds` field: [3](#0-2) 

Therefore, no on-chain SNS governance proposal can ever change this delay. The identical pattern exists in NNS Governance:

```rust
const DISBURSEMENT_DELAY_SECONDS: u64 = ONE_DAY_SECONDS * 7;
``` [4](#0-3) 

This constant is also absent from `NetworkEconomics`, so `ManageNetworkEconomics` proposals cannot update it either. [5](#0-4) 

---

### Impact Explanation

Any neuron holder who calls `disburse_maturity` on an SNS governance canister will have their disbursement locked for exactly 7 days, with no ability for the SNS community to shorten or lengthen this window via a `ManageNervousSystemParameters` proposal. If an SNS community decides the delay should be 0 (to disable it), 14 days (for additional security), or any other value, they cannot do so through the designated governance mechanism. The only recourse is a full canister upgrade — a significantly heavier operation that requires NNS approval for SNS-W-managed canisters. This breaks the governance contract that `NervousSystemParameters` is supposed to provide: the ability to tune protocol parameters without a code deployment.

For NNS Governance, the same hardcoded 7-day delay applies to all ICP neuron holders who initiate maturity disbursements, and NNS governance cannot adjust it via `ManageNetworkEconomics`. [6](#0-5) 

---

### Likelihood Explanation

This is a reachable, deterministic issue. Any unprivileged neuron holder can call `disburse_maturity` and will be subject to the hardcoded delay. The missing governance hook is not gated by any flag or feature toggle — it is structurally absent from `NervousSystemParameters`. Any SNS that wishes to adjust this parameter will discover the limitation immediately upon attempting to submit a `ManageNervousSystemParameters` proposal. [7](#0-6) 

---

### Recommendation

Add `maturity_disbursement_delay_seconds` as an optional field to `NervousSystemParameters` in `governance.proto`, with appropriate floor/ceiling bounds (e.g., 0 to 30 days). Update `disburse_maturity()` to read the delay from `self.nervous_system_parameters_or_panic().maturity_disbursement_delay_seconds` with a fallback to `MATURITY_DISBURSEMENT_DELAY_SECONDS`. Validate the new field in `NervousSystemParameters::validate()` similarly to how other duration parameters are validated. Apply the same fix to NNS Governance by adding a `maturity_disbursement_delay_seconds` field to `NetworkEconomics`. [8](#0-7) 

---

### Proof of Concept

1. An SNS community submits a `ManageNervousSystemParameters` proposal with any value for `maturity_disbursement_delay_seconds`.
2. The proposal fails at the Candid/protobuf decoding stage because no such field exists in `NervousSystemParameters`.
3. Alternatively, the community submits a valid `ManageNervousSystemParameters` proposal omitting the field — the delay remains 7 days regardless.
4. A neuron holder calls `disburse_maturity` — the disbursement is always scheduled at `now + 604800` seconds, immutably.
5. There is no `update` endpoint on the SNS governance canister that accepts a new delay value; the only path to change it is `UpgradeSnsToNextVersion` or `UpgradeSnsControlledCanister`, requiring a full Wasm redeployment. [1](#0-0) [9](#0-8)

### Citations

**File:** rs/sns/governance/src/governance.rs (L162-162)
```rust
pub const MATURITY_DISBURSEMENT_DELAY_SECONDS: u64 = 7 * 24 * 3600;
```

**File:** rs/sns/governance/src/governance.rs (L1680-1688)
```rust
        let now_seconds = self.env.now();
        let disbursement_in_progress = DisburseMaturityInProgress {
            amount_e8s: maturity_to_deduct,
            timestamp_of_disbursement_seconds: now_seconds,
            account_to_disburse_to: Some(to_account_proto),
            finalize_disbursement_timestamp_seconds: Some(
                now_seconds + MATURITY_DISBURSEMENT_DELAY_SECONDS,
            ),
        };
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L1118-1265)
```text
message NervousSystemParameters {
  // The number of e8s (10e-8 of a token) that a rejected
  // proposal costs the proposer.
  optional uint64 reject_cost_e8s = 1;

  // The minimum number of e8s (10e-8 of a token) that can be staked in a neuron.
  //
  // To ensure that staking and disbursing of the neuron work, the chosen value
  // must be larger than the transaction_fee_e8s.
  optional uint64 neuron_minimum_stake_e8s = 2;

  // The transaction fee that must be paid for ledger transactions (except
  // minting and burning governance tokens).
  optional uint64 transaction_fee_e8s = 3;

  // The maximum number of proposals to keep, per action. When the
  // total number of proposals for a given action is greater than this
  // number, the oldest proposals that have reached final decision state
  // (rejected, executed, or failed) and final rewards status state
  // (settled) may be deleted.
  //
  // The number must be larger than zero and at most be as large as the
  // defined ceiling MAX_PROPOSALS_TO_KEEP_PER_ACTION_CEILING.
  optional uint32 max_proposals_to_keep_per_action = 4;

  // The initial voting period of a newly created proposal.
  // A proposal's voting period may then be further increased during
  // a proposal's lifecycle due to the wait-for-quiet algorithm.
  //
  // The voting period must be between (inclusive) the defined floor
  // INITIAL_VOTING_PERIOD_SECONDS_FLOOR and ceiling
  // INITIAL_VOTING_PERIOD_SECONDS_CEILING.
  optional uint64 initial_voting_period_seconds = 5;

  // The wait for quiet algorithm extends the voting period of a proposal when
  // there is a flip in the majority vote during the proposal's voting period.
  // This parameter determines the maximum time period that the voting period
  // may be extended after a flip. If there is a flip at the very end of the
  // original proposal deadline, the remaining time will be set to this parameter.
  // If there is a flip before or after the original deadline, the deadline will be
  // extended by somewhat less than this parameter.
  // The maximum total voting period extension is 2 * wait_for_quiet_deadline_increase_seconds.
  // For more information, see the wiki page on the wait-for-quiet algorithm:
  // https://internetcomputer.org/how-it-works/network-nervous-system-nns/#voting-rules
  optional uint64 wait_for_quiet_deadline_increase_seconds = 18;

  // TODO NNS1-2169: This field currently has no effect.
  // TODO NNS1-2169: Design and implement this feature.
  //
  // The set of default followees that every newly created neuron will follow
  // per function. This is specified as a mapping of proposal functions to followees.
  //
  // If unset, neurons will have no followees by default.
  // The set of followees for each function can be at most of size
  // max_followees_per_function.
  optional DefaultFollowees default_followees = 6;

  // The maximum number of allowed neurons. When this maximum is reached, no new
  // neurons will be created until some are removed.
  //
  // This number must be larger than zero and at most as large as the defined
  // ceiling MAX_NUMBER_OF_NEURONS_CEILING.
  optional uint64 max_number_of_neurons = 7;

  // The minimum dissolve delay a neuron must have to be eligible to vote.
  //
  // The chosen value must be smaller than max_dissolve_delay_seconds.
  optional uint64 neuron_minimum_dissolve_delay_to_vote_seconds = 8;

  // The maximum number of followees each neuron can establish for each nervous system function.
  //
  // This number can be at most as large as the defined ceiling
  // MAX_FOLLOWEES_PER_FUNCTION_CEILING.
  optional uint64 max_followees_per_function = 9;

  // The maximum dissolve delay that a neuron can have. That is, the maximum
  // that a neuron's dissolve delay can be increased to. The maximum is also enforced
  // when saturating the dissolve delay bonus in the voting power computation.
  optional uint64 max_dissolve_delay_seconds = 10;

  // The age of a neuron that saturates the age bonus for the voting power computation.
  optional uint64 max_neuron_age_for_age_bonus = 12;

  // See voting_rewards_parameters. (This is here to mollify pre-commit.)
  reserved "reward_distribution_period_seconds";
  reserved 13;

  // The max number of proposals for which ballots are still stored, i.e.,
  // unsettled proposals. If this number of proposals is reached, new proposals
  // can only be added in exceptional cases (for few proposals it is defined
  // that they are allowed even if resources are low to guarantee that the relevant
  // canisters can be upgraded).
  //
  // This number must be larger than zero and at most as large as the defined
  // ceiling MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS_CEILING.
  optional uint64 max_number_of_proposals_with_ballots = 14;

  // The default set of neuron permissions granted to the principal claiming a neuron.
  optional NeuronPermissionList neuron_claimer_permissions = 15;

  // The superset of neuron permissions a principal with permission
  // `NeuronPermissionType::ManagePrincipals` for a given neuron can grant to another
  // principal for this same neuron.
  // If this set changes via a ManageNervousSystemParameters proposal, previous
  // neurons' permissions will be unchanged and only newly granted permissions will be affected.
  optional NeuronPermissionList neuron_grantable_permissions = 16;

  // The maximum number of principals that can have permissions for a neuron
  optional uint64 max_number_of_principals_per_neuron = 17;

  // When this field is not populated, voting rewards are "disabled". Once this
  // is set, it probably should not be changed, because the results would
  // probably be pretty confusing.
  VotingRewardsParameters voting_rewards_parameters = 19;

  // E.g. if a large dissolve delay can double the voting power of a neuron,
  // then this field would have a value of 100, indicating a maximum of
  // 100% additional voting power.
  //
  // For no bonus, this should be set to 0.
  //
  // To achieve functionality equivalent to NNS, this should be set to 100.
  optional uint64 max_dissolve_delay_bonus_percentage = 20;

  // Analogous to the previous field (see the previous comment),
  // but this one relates to neuron age instead of dissolve delay.
  //
  // To achieve functionality equivalent to NNS, this should be set to 25.
  optional uint64 max_age_bonus_percentage = 21;

  // By default, maturity modulation is enabled; however, an SNS can use this
  // field to disable it. When disabled, this canister will still poll the
  // Cycles Minting Canister (CMC), and store the value received therefrom.
  // However, the fetched value does not get used when this is set to true.
  //
  // The reason we call this "disabled" instead of (positive) "enabled" is so
  // that the PB default (bool fields are false) and our application default
  // (enabled) agree.
  optional bool maturity_modulation_disabled = 22;

  // Whether to automatically advance the SNS target version after a new upgrade is published
  // by the NNS. If not specified, defaults to false for backward compatibility.
  optional bool automatically_advance_target_version = 23;

  // Custom proposal criticality configuration. Allows specifying additional native function IDs
  // that should be treated as critical. If not specified, defaults to None (no custom criticality).
  optional CustomProposalCriticality custom_proposal_criticality = 24;
}
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L36-40)
```rust
/// The delay in seconds between initiating a maturity disbursement and the actual disbursement.
const DISBURSEMENT_DELAY_SECONDS: u64 = ONE_DAY_SECONDS * 7;
/// The maximum number of disbursements in a neuron. This makes it possible to do daily
/// disbursements after every reward event (as 10 > 7).
const MAX_NUM_DISBURSEMENTS: usize = 10;
```

**File:** rs/nns/governance/canister/governance.did (L693-706)
```text
type NetworkEconomics = record {
  neuron_minimum_stake_e8s : nat64;
  max_proposals_to_keep_per_topic : nat32;
  neuron_management_fee_per_proposal_e8s : nat64;
  reject_cost_e8s : nat64;
  transaction_fee_e8s : nat64;
  neuron_spawn_dissolve_delay_seconds : nat64;
  minimum_icp_xdr_rate : nat64;
  maximum_node_provider_rewards_e8s : nat64;
  neurons_fund_economics : opt NeuronsFundEconomics;

  // Parameters that affect the voting power of neurons.
  voting_power_economics : opt VotingPowerEconomics;
};
```
