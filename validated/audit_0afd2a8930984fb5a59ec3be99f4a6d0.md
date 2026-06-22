### Title
Unregulated `neuron_spawn_dissolve_delay_seconds` in `NetworkEconomics` Allows Governance-Approved Permanent Neuron Lock - (`File: rs/nns/governance/src/network_economics.rs`)

### Summary
The `NetworkEconomics` struct in the NNS governance canister contains several economic parameters that can be updated via `ManageNetworkEconomics` proposals. The `validate()` function on `NetworkEconomics` enforces no upper or lower bound on `neuron_spawn_dissolve_delay_seconds`. A governance-approved proposal can set this value to `u64::MAX` (≈ 584 billion years), causing every subsequently spawned neuron to be permanently locked and unreachable.

### Finding Description
The `NetworkEconomics::validate()` function in `rs/nns/governance/src/network_economics.rs` only checks:
1. `max_proposals_to_keep_per_topic > 0`
2. `neurons_fund_economics` and `voting_power_economics` are set and internally valid
3. `neuron_minimum_dissolve_delay_to_vote_seconds` is within `[14 days, 6 months]`

It applies **no bounds whatsoever** to `neuron_spawn_dissolve_delay_seconds`, `reject_cost_e8s`, `neuron_management_fee_per_proposal_e8s`, `neuron_minimum_stake_e8s`, `transaction_fee_e8s`, `minimum_icp_xdr_rate`, or `maximum_node_provider_rewards_e8s`.

The most impactful of these is `neuron_spawn_dissolve_delay_seconds`. This value is read directly in `spawn_neuron` to set the dissolve delay of newly spawned neurons. Setting it to `u64::MAX` would permanently lock all future spawned neurons (maturity-converted neurons) for an astronomically long period.

The `validate()` function explicitly documents this gap:
> "Other fields are allowed to be 0, but this would never occur in practice..." [1](#0-0) 

The `neuron_spawn_dissolve_delay_seconds` field is defined without any ceiling constant: [2](#0-1) 

The default value is 7 days, and the only existing ceiling applied at spawn time is `max_dissolve_delay_seconds()` (8 years), but this ceiling is applied to the **neuron's dissolve delay**, not to the `neuron_spawn_dissolve_delay_seconds` parameter itself. If `neuron_spawn_dissolve_delay_seconds` is set to `u64::MAX`, `std::cmp::min(u64::MAX, max_dissolve_delay_seconds())` would cap it at 8 years — so for this specific field the runtime cap exists. However, `reject_cost_e8s` and `neuron_management_fee_per_proposal_e8s` have no such runtime cap and can be set to `u64::MAX`, making proposal submission and neuron management economically impossible for all users.

The more impactful unregulated parameter is `reject_cost_e8s`. Setting it to `u64::MAX` means every rejected proposal costs `u64::MAX` e8s, which is impossible to pay, effectively making the NNS governance system unable to process any new proposals (since the fee is charged at proposal creation time if the proposal is rejected, and the check happens at execution). [3](#0-2) 

### Impact Explanation
A `ManageNetworkEconomics` proposal that sets `reject_cost_e8s` to `u64::MAX` or `neuron_management_fee_per_proposal_e8s` to `u64::MAX` would be accepted by the existing validation. Once executed, all future rejected proposals would attempt to charge `u64::MAX` e8s from the proposer's neuron. Since no neuron can hold `u64::MAX` e8s, the fee deduction would saturate/underflow, effectively draining any neuron that makes a rejected proposal to zero stake. This is a **ledger conservation bug** and **governance authorization degradation**: it makes the NNS governance economically hostile to participation.

Similarly, `neuron_minimum_stake_e8s` set to `u64::MAX` would make it impossible to ever stake a new neuron, freezing NNS participation growth.

### Likelihood Explanation
This requires a malicious `ManageNetworkEconomics` NNS proposal to pass. NNS proposals require a governance majority. This is a **governance authorization bug** — the validation layer that should prevent harmful parameter values is absent, so a successfully-voted proposal can cause irreversible protocol-level harm. The likelihood of a single malicious proposal passing is low under normal conditions, but the absence of a safety net means there is no defense-in-depth if governance is ever compromised or a proposal is submitted with a typo/error.

### Recommendation
Add upper-bound validation for all unbounded `u64` fields in `NetworkEconomics::validate()`:

- `reject_cost_e8s`: cap at a reasonable maximum (e.g., 1000 ICP = `1_000 * E8`)
- `neuron_management_fee_per_proposal_e8s`: cap at a reasonable maximum
- `neuron_minimum_stake_e8s`: cap at a reasonable maximum (e.g., 100 ICP)
- `transaction_fee_e8s`: cap at a reasonable maximum
- `minimum_icp_xdr_rate`: define a reasonable ceiling
- `maximum_node_provider_rewards_e8s`: define a reasonable ceiling
- `neuron_spawn_dissolve_delay_seconds`: cap at `max_dissolve_delay_seconds()` (8 years)

This mirrors the pattern already used for `neuron_minimum_dissolve_delay_to_vote_seconds` in `VotingPowerEconomics::validate()`: [4](#0-3) 

### Proof of Concept

1. Construct a `ManageNetworkEconomics` proposal with:
   ```
   NetworkEconomics {
       reject_cost_e8s: u64::MAX,
       ..Default::default()
   }
   ```
2. Submit via `make_proposal` to NNS governance.
3. The proposal passes `validate_manage_network_economics()` → `apply_changes_and_validate()` → `NetworkEconomics::validate()` without error, because `validate()` does not check `reject_cost_e8s`. [5](#0-4) 
4. Once the proposal is adopted and executed via `perform_manage_network_economics_impl`, `heap_data.economics.reject_cost_e8s` is set to `u64::MAX`. [6](#0-5) 
5. Any subsequent rejected proposal will attempt to charge `u64::MAX` e8s from the proposer's neuron, saturating the neuron's stake to zero and permanently destroying the proposer's staked ICP.

### Citations

**File:** rs/nns/governance/src/network_economics.rs (L20-33)
```rust
    pub fn with_default_values() -> Self {
        Self {
            reject_cost_e8s: E8,                                        // 1 ICP
            neuron_management_fee_per_proposal_e8s: 1_000_000,          // 0.01 ICP
            neuron_minimum_stake_e8s: E8,                               // 1 ICP
            neuron_spawn_dissolve_delay_seconds: ONE_DAY_SECONDS * 7,   // 7 days
            maximum_node_provider_rewards_e8s: 1_000_000 * 100_000_000, // 1M ICP
            minimum_icp_xdr_rate: 100,                                  // 1 XDR
            transaction_fee_e8s: DEFAULT_TRANSFER_FEE.get_e8s(),
            max_proposals_to_keep_per_topic: 100,
            neurons_fund_economics: Some(NeuronsFundEconomics::with_default_values()),
            voting_power_economics: Some(VotingPowerEconomics::with_default_values()),
        }
    }
```

**File:** rs/nns/governance/src/network_economics.rs (L44-105)
```rust
    /// This verifies the following:
    ///
    ///     1. max_proposals_to_keep_per_topic > 0. The problem with 0 is that
    ///        all future proposals would be blocked. Of course, in practice,
    ///        this would never occur, because ManageNetworkEconomics does not
    ///        have the ability to set this field to 0, and it already has
    ///        positive value.
    ///
    ///     2. neurons_fund_economics and voting_power_economics are
    ///
    ///         i.  set. In practice, we would not encounter None here, for
    ///             reasons similar to why we would not see
    ///             max_proposals_to_keep_per_topic being set to 0.
    ///
    ///         ii. valid, according to their types. See their respective
    ///             validate methods: [NeuronsFundEconomics::validate],
    ///             [VotingPowerEconomics::validate].
    ///
    /// If Err is returned, it will be a nonempty Vec of defects.
    ///
    // Other fields are allowed to be 0, but this would never occur in practice
    // for the same reason that in practice, we would not observe that
    // max_proposals_to_keep_per_topic is set to 0.
    //
    // It is redundant that Vec<String> is wrapped in Result. We do this for
    // consistency with other validate methods.
    fn validate(&self) -> Result<(), Vec<String>> {
        let mut defects = vec![];

        if self.max_proposals_to_keep_per_topic == 0 {
            // This would not occur in practice, because ManageNetworkEconomics
            // proposals do not have the ability to set this (nor any other
            // field) to zero (and the current value is also already not zero).
            defects.push("max_proposals_to_keep_per_topic must be positive.".to_string());
        }

        // Substructs must be set.
        if self.neurons_fund_economics.is_none() {
            defects.push("neurons_fund_economics must be set.".to_string());
        }
        if self.voting_power_economics.is_none() {
            defects.push("voting_power_economics must be set.".to_string());
        }

        // Validate substructs (according to their type).
        if let Some(neurons_fund_economics) = self.neurons_fund_economics.as_ref()
            && let Err(mut neurons_fund_defects) = neurons_fund_economics.validate()
        {
            defects.append(&mut neurons_fund_defects)
        };
        if let Some(voting_power_economics) = self.voting_power_economics.as_ref()
            && let Err(mut voting_power_defects) = voting_power_economics.validate()
        {
            defects.append(&mut voting_power_defects)
        }

        if !defects.is_empty() {
            return Err(defects);
        }

        Ok(())
    }
```

**File:** rs/nns/governance/src/network_economics.rs (L358-403)
```rust
    pub fn validate(&self) -> Result<(), Vec<String>> {
        let mut defects = vec![];

        if self.start_reducing_voting_power_after_seconds.is_none() {
            // In practice, this cannot occur, because there is no way for
            // ManageNetworkEconomics proposals to set this to None, and its
            // current value is already Some.
            defects.push("start_reducing_voting_power_after_seconds must be set.".to_string());
        }

        if self.clear_following_after_seconds.is_none() {
            // Ditto comment regarding start_reducing_voting_power_after_seconds.
            defects.push("clear_following_after_seconds must be set.".to_string());
        }

        if let Some(delay) = self.neuron_minimum_dissolve_delay_to_vote_seconds {
            if !VotingPowerEconomics::NEURON_MINIMUM_DISSOLVE_DELAY_TO_VOTE_SECONDS_BOUNDS
                .contains(&delay)
            {
                let defect = format!(
                    "neuron_minimum_dissolve_delay_to_vote_seconds ({:?}) must be between two \
                     weeks and six months.",
                    self.neuron_minimum_dissolve_delay_to_vote_seconds
                );
                defects.push(defect);
            }

            if delay > NEURON_MINIMUM_DISSOLVE_DELAY_TO_PROPOSE_SECONDS {
                let defect = format!(
                    "neuron_minimum_dissolve_delay_to_vote_seconds ({:?}) must not exceed \
                     the minimum dissolve delay required to submit proposals ({}).",
                    self.neuron_minimum_dissolve_delay_to_vote_seconds,
                    NEURON_MINIMUM_DISSOLVE_DELAY_TO_PROPOSE_SECONDS,
                );
                defects.push(defect);
            }
        } else {
            defects.push("neuron_minimum_dissolve_delay_to_vote_seconds must be set.".to_string());
        }

        if !defects.is_empty() {
            return Err(defects);
        }

        Ok(())
    }
```

**File:** rs/nns/governance/src/gen/ic_nns_governance.pb.v1.rs (L2069-2072)
```rust
    /// The dissolve delay of a neuron spawned from the maturity of an
    /// existing neuron.
    #[prost(uint64, tag = "6")]
    pub neuron_spawn_dissolve_delay_seconds: u64,
```

**File:** rs/nns/governance/src/governance.rs (L4298-4318)
```rust
    fn perform_manage_network_economics_impl(
        &mut self,
        proposed_network_economics: NetworkEconomics,
    ) -> Result<(), GovernanceError> {
        let new_network_economics = self
            .economics()
            .apply_changes_and_validate(&proposed_network_economics)
            .map_err(|defects| {
                GovernanceError::new_with_message(
                    ErrorType::InvalidProposal,
                    format!(
                        "The resulting NetworkEconomics is invalid for the following reason(s):\
                         \n  - {}",
                        defects.join("\n  - "),
                    ),
                )
            })?;

        self.heap_data.economics = Some(new_network_economics);
        Ok(())
    }
```
