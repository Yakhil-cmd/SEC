### Title
NNS/SNS Neuron Staking Locking Requirement Incentivizes Uncontrolled Third-Party Liquid Staking Protocols, Exposing Users to Token Loss - (File: rs/nns/governance/src/neuron_store/voting_power.rs, rs/sns/init/src/lib.rs)

---

### Summary

The NNS and SNS governance canisters require tokens to be locked in neurons with a minimum dissolve delay before they are eligible to earn voting rewards. This design creates a direct economic incentive for users to deposit their tokens into third-party liquid staking protocols (e.g., WaterNeuron on mainnet) to maintain liquidity while still earning rewards. The SNS governance compounds this by defaulting `neuron_grantable_permissions` to the full set of all permissions — including `Disburse` and `DisburseMaturity` — allowing neuron owners to delegate full withdrawal authority to any third-party canister principal. If such a canister is buggy or malicious, users lose their staked tokens with no recourse through NNS/SNS governance.

---

### Finding Description

**Root cause 1 — NNS locking requirement for rewards:**

In `rs/nns/governance/src/neuron_store/voting_power.rs`, the `compute_voting_power_snapshot_for_standard_proposal` function explicitly excludes neurons whose dissolve delay falls below `min_dissolve_delay_seconds` from receiving any voting power or rewards:

```rust
if neuron.is_inactive(now_seconds)
    || neuron.dissolve_delay_seconds(now_seconds) < min_dissolve_delay_seconds
{
    return;
}
``` [1](#0-0) 

The default minimum dissolve delay is 6 months (recently reduced to 2 weeks under Mission 70), enforced in `rs/nns/governance/src/network_economics.rs`: [2](#0-1) 

The NNS governance documentation in `rs/nns/governance/src/lib.rs` explicitly states that ICP must be locked to earn rewards: [3](#0-2) 

**Root cause 2 — SNS defaults all permissions as grantable:**

In `rs/sns/init/src/lib.rs`, the `get_nervous_system_parameters` function initializes every new SNS with `neuron_grantable_permissions` set to the complete set of all `NeuronPermissionType` values:

```rust
let all_permissions = NeuronPermissionList {
    permissions: NeuronPermissionType::all(),
};
// ...
NervousSystemParameters {
    neuron_claimer_permissions: Some(all_permissions.clone()),
    neuron_grantable_permissions: Some(all_permissions),
    // ...
}
``` [4](#0-3) 

This means any SNS neuron owner can grant `Disburse` (permission 5) and `DisburseMaturity` (permission 8) to an arbitrary third-party canister principal: [5](#0-4) 

The SNS governance enforces the same dissolve-delay-gated reward eligibility as NNS: [6](#0-5) 

**Combined exploit path:**

Because rewards require locking, users seeking liquidity are pushed toward third-party liquid staking canisters. Because SNS defaults all permissions as grantable, a user can grant `Disburse` to such a canister. The canister then holds full withdrawal authority over the user's locked SNS tokens. Any bug or malicious logic in that canister results in permanent loss of the user's tokens — with no NNS/SNS governance mechanism to recover them.

---

### Impact Explanation

A user who deposits SNS tokens into a third-party liquid staking canister and grants it `Disburse` or `DisburseMaturity` permission loses all their staked tokens if the canister is exploited. The NNS governance design creates the same incentive for ICP (WaterNeuron is a live example on mainnet). The SNS `neuron_grantable_permissions` default of all permissions makes the delegation of full withdrawal authority a single `AddNeuronPermissions` call away. Token loss is permanent and irreversible at the governance layer.

---

### Likelihood Explanation

This is not theoretical. WaterNeuron is a live, publicly deployed liquid staking protocol for ICP on mainnet. Multiple SNS projects have liquid staking protocols or are building them. The economic incentive (earn rewards without sacrificing liquidity) is strong and well-understood. The SNS code path that enables granting `Disburse` to a canister principal is fully functional and requires no special privileges — any neuron owner can do it.

---

### Recommendation

1. **NNS/SNS**: Consider implementing a native liquid representation of staked neurons (analogous to applying rewards to the balance without requiring a separate lock), so users do not need third-party proxies to maintain liquidity while earning rewards.
2. **SNS init defaults**: Change the default `neuron_grantable_permissions` in `rs/sns/init/src/lib.rs` to exclude `Disburse` and `DisburseMaturity`. These permissions grant full withdrawal authority and should not be grantable to arbitrary third-party canisters by default.
3. **Documentation**: Explicitly warn SNS deployers and NNS stakers that granting `Disburse`/`DisburseMaturity` to a canister principal transfers full custody of staked tokens to that canister's logic, which is outside NNS/SNS governance control.

---

### Proof of Concept

1. Alice deploys a liquid staking canister `LSTC` for an SNS token. `LSTC` accepts SNS token deposits, locks them in neurons, and issues liquid `xSNS` tokens to depositors.
2. Bob holds SNS tokens and wants to earn voting rewards without a long lock-up. He deposits into `LSTC`, which stakes his tokens in a neuron and grants `LSTC`'s canister principal `Disburse` + `DisburseMaturity` permissions on that neuron. Bob receives `xSNS`.
3. The SNS governance distributes voting rewards to the neuron (locked, dissolve delay ≥ `neuron_minimum_dissolve_delay_to_vote_seconds`). `LSTC` accumulates maturity.
4. A hacker finds a reentrancy or authorization bug in `LSTC`. They call `LSTC`'s redeem function in a way that triggers `Disburse` on the underlying SNS neuron to the hacker's account.
5. The SNS governance canister processes the `Disburse` call — it is authorized because `LSTC`'s principal holds the `Disburse` permission — and transfers Bob's staked SNS tokens to the hacker.
6. Bob's `xSNS` tokens are now worthless. The SNS governance has no mechanism to reverse the transfer. Bob loses all his holdings.

The NNS governance's `manage_neuron` entry point is the reachable ingress path: [7](#0-6) 

The SNS `add_neuron_permissions` path that enables granting `Disburse` to a canister: [8](#0-7)

### Citations

**File:** rs/nns/governance/src/neuron_store/voting_power.rs (L144-149)
```rust
        let mut process_neuron = |neuron: &Neuron| {
            if neuron.is_inactive(now_seconds)
                || neuron.dissolve_delay_seconds(now_seconds) < min_dissolve_delay_seconds
            {
                return;
            }
```

**File:** rs/nns/governance/src/network_economics.rs (L278-283)
```rust
    pub const DEFAULT_NEURON_MINIMUM_DISSOLVE_DELAY_TO_VOTE_SECONDS: u64 = 6 * ONE_MONTH_SECONDS;

    /// The default value for `neuron_minimum_dissolve_delay_to_vote_seconds` once the mission 70
    /// voting rewards feature is enabled. Two weeks instead of six months.
    pub const MISSION_70_DEFAULT_NEURON_MINIMUM_DISSOLVE_DELAY_TO_VOTE_SECONDS: u64 =
        14 * ONE_DAY_SECONDS;
```

**File:** rs/nns/governance/src/lib.rs (L29-46)
```rust
//! processed. To prevent users spamming the NNS, a fee is levied on
//! the neuron that submitted a proposal if it is rejected.
//!
//! The NNS decides whether to adopt or reject proposals by watching
//! how neurons emit votes. Anyone can create a neuron by locking
//! balances of “ICP governance tokens”, a special native utility
//! token that is hosted on a ledger inside the NNS. When a user
//! creates a neuron, the locked balance of ICP can only be unlocked
//! by fully dissolving (“destroying”) the neuron. Users are
//! incentivized to create neurons because they earn rewards when they
//! vote on proposals. Rewards take the form of newly minted ICP that
//! are created by the NNS. The quantity of ICP rewards disbursed to a
//! neuron derive from such factors as the size of the locked balance,
//! the minimum lockup period remaining (the “dissolve delay”), the
//! neuron’s “age”, the proportion of possible votes it has correctly
//! participated in, and the sum of voting activity across all
//! neurons, since the overall total rewards disbursed is capped and
//! must be divided.
```

**File:** rs/sns/init/src/lib.rs (L759-814)
```rust
        let all_permissions = NeuronPermissionList {
            permissions: NeuronPermissionType::all(),
        };

        let SnsInitPayload {
            transaction_fee_e8s,
            token_name: _,
            token_symbol: _,
            proposal_reject_cost_e8s: reject_cost_e8s,
            neuron_minimum_stake_e8s,
            fallback_controller_principal_ids: _,
            logo: _,
            url: _,
            name: _,
            description: _,
            neuron_minimum_dissolve_delay_to_vote_seconds,
            reward_rate_transition_duration_seconds,
            initial_reward_rate_basis_points,
            final_reward_rate_basis_points,
            initial_token_distribution: _,
            max_dissolve_delay_seconds,
            max_neuron_age_seconds_for_age_bonus: max_neuron_age_for_age_bonus,
            max_dissolve_delay_bonus_percentage,
            max_age_bonus_percentage,
            initial_voting_period_seconds,
            wait_for_quiet_deadline_increase_seconds,
            dapp_canisters: _,
            confirmation_text: _,
            restricted_countries: _,
            min_participants: _,
            min_icp_e8s: _,
            max_icp_e8s: _,
            min_direct_participation_icp_e8s: _,
            max_direct_participation_icp_e8s: _,
            min_participant_icp_e8s: _,
            max_participant_icp_e8s: _,
            swap_start_timestamp_seconds: _,
            swap_due_timestamp_seconds: _,
            neuron_basket_construction_parameters: _,
            nns_proposal_id: _,
            token_logo: _,
            neurons_fund_participation_constraints: _,
            neurons_fund_participation: _,
            custom_proposal_criticality,
        } = self.clone();

        let voting_rewards_parameters = Some(VotingRewardsParameters {
            reward_rate_transition_duration_seconds,
            initial_reward_rate_basis_points,
            final_reward_rate_basis_points,
            ..nervous_system_parameters.voting_rewards_parameters.unwrap()
        });

        NervousSystemParameters {
            neuron_claimer_permissions: Some(all_permissions.clone()),
            neuron_grantable_permissions: Some(all_permissions),
```

**File:** rs/sns/governance/api/src/ic_sns_governance.pb.v1.rs (L2688-2698)
```rust
    /// The principal has permission to disburse the neuron.
    Disburse = 5,
    /// The principal has permission to split the neuron.
    Split = 6,
    /// The principal has permission to merge the neuron's maturity into
    /// the neuron's stake.
    MergeMaturity = 7,
    /// The principal has permission to disburse the neuron's maturity to a
    /// given ledger account.
    DisburseMaturity = 8,
    /// The principal has permission to stake the neuron's maturity.
```

**File:** rs/sns/governance/src/governance.rs (L4570-4634)
```rust
    fn add_neuron_permissions(
        &mut self,
        neuron_id: &NeuronId,
        caller: &PrincipalId,
        add_neuron_permissions: &AddNeuronPermissions,
    ) -> Result<(), GovernanceError> {
        let neuron = self.get_neuron_result(neuron_id)?;

        let permissions_to_add = add_neuron_permissions
            .permissions_to_add
            .as_ref()
            .ok_or_else(|| {
                GovernanceError::new_with_message(
                    ErrorType::InvalidCommand,
                    "AddNeuronPermissions command must provide permissions to add",
                )
            })?;

        // A simple check to prevent DoS attack with large number of permission changes.
        if permissions_to_add.permissions.len() > NeuronPermissionType::all().len() {
            return Err(GovernanceError::new_with_message(
                ErrorType::InvalidCommand,
                "AddNeuronPermissions command provided more permissions than exist in the system",
            ));
        }

        neuron
            .check_principal_authorized_to_change_permissions(caller, permissions_to_add.clone())?;

        self.nervous_system_parameters_or_panic()
            .check_permissions_are_grantable(permissions_to_add)?;

        let principal_id = add_neuron_permissions.principal_id.ok_or_else(|| {
            GovernanceError::new_with_message(
                ErrorType::InvalidCommand,
                "AddNeuronPermissions command must provide a PrincipalId to add permissions to",
            )
        })?;

        let existing_permissions = neuron
            .permissions
            .iter()
            .find(|permission| permission.principal == Some(principal_id));

        let max_number_of_principals_per_neuron = self
            .nervous_system_parameters_or_panic()
            .max_number_of_principals_per_neuron
            .expect("NervousSystemParameters.max_number_of_principals_per_neuron must be present");

        // If the PrincipalId does not already exist in the neuron, make sure it can be added
        if existing_permissions.is_none()
            && neuron.permissions.len() == max_number_of_principals_per_neuron as usize
        {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!(
                    "Cannot add permission to neuron. Max \
                    number of principals reached {max_number_of_principals_per_neuron}"
                ),
            ));
        }

        // Re-borrow the neuron mutably to update now that the preconditions have been met
        self.get_neuron_result_mut(neuron_id)?
            .add_permissions_for_principal(principal_id, permissions_to_add.permissions.clone());
```

**File:** rs/sns/governance/src/governance.rs (L5255-5261)
```rust
        for (k, v) in self.proto.neurons.iter() {
            // If this neuron is eligible to vote, record its
            // voting power at the time of proposal creation (now).
            if v.dissolve_delay_seconds(now_seconds) < min_dissolve_delay_for_vote {
                // Not eligible due to dissolve delay.
                continue;
            }
```

**File:** rs/nns/governance/src/governance.rs (L6081-6089)
```rust
    pub async fn manage_neuron(
        &mut self,
        caller: &PrincipalId,
        mgmt: &ManageNeuron,
    ) -> ManageNeuronResponse {
        self.manage_neuron_internal(caller, mgmt)
            .await
            .unwrap_or_else(ManageNeuronResponse::error)
    }
```
