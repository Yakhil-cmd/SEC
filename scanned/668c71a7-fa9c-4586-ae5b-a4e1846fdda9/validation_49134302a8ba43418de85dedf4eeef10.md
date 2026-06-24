### Title
Missing Lower-Bound Validation on `VotingPowerEconomics` Timing Parameters Allows Zero-Value Governance Disruption - (File: rs/nns/governance/src/network_economics.rs)

---

### Summary

The NNS governance canister's `VotingPowerEconomics` struct contains two timing parameters — `start_reducing_voting_power_after_seconds` and `clear_following_after_seconds` — that are explicitly documented as "allowed to be set to 0" in the validation code. A `ManageNetworkEconomics` proposal passed by NNS governance can set these to zero, causing all neurons to immediately and permanently lose their deciding voting power and have their following cleared on every heartbeat, effectively breaking NNS governance liveness.

---

### Finding Description

`VotingPowerEconomics` in the NNS governance canister holds two timing parameters that control the voting power decay window:

- `start_reducing_voting_power_after_seconds`: the grace period before a neuron's deciding voting power starts decreasing.
- `clear_following_after_seconds`: the additional window after which voting power reaches zero and following is cleared.

The `validate()` method for `VotingPowerEconomics` explicitly states:

> "They are allowed to be set to 0 though." [1](#0-0) 

The validation only checks that the fields are `Some(...)`, not that they are above any meaningful minimum. Both fields can be set to `Some(0)` and pass validation. [2](#0-1) 

The `deciding_voting_power_adjustment_factor_function` builds a `LinearMap` from the range `begin..end` where `begin = start_reducing_voting_power_after_seconds` and `end = begin.saturating_add(clear_following_after_seconds)`. If both are 0, the range is `0..0` (empty), and the `clamp` to `[0, 1]` causes every neuron's deciding voting power to be 0 immediately, regardless of when they last refreshed. [3](#0-2) 

The `prune_following` logic in neuron types uses these same parameters. With both set to 0, every neuron is considered stale on every call, causing all non-`NeuronManagement` following to be cleared continuously. [4](#0-3) 

The `ManageNetworkEconomics` proposal path validates and applies changes via `apply_changes_and_validate`, which calls `VotingPowerEconomics::validate()` — but that validation does not reject zero values. [5](#0-4) [6](#0-5) 

---

### Impact Explanation

If `start_reducing_voting_power_after_seconds = 0` and `clear_following_after_seconds = 0` are set via a `ManageNetworkEconomics` proposal:

1. **All neurons immediately have 0 deciding voting power** — no neuron can contribute to any proposal's tally, since the decay window starts and ends at time 0.
2. **All following (except `NeuronManagement`) is cleared on every periodic task** — neurons cannot rely on liquid democracy to vote.
3. **New proposals cannot reach quorum** — since all deciding voting power is 0, no proposal can be adopted or rejected by majority, breaking governance liveness.
4. **The NNS becomes unable to self-correct** — a subsequent `ManageNetworkEconomics` proposal to fix the parameters cannot pass because no neuron has deciding voting power to vote on it.

This is a governance authorization / liveness bug: the NNS governance canister can be rendered permanently non-functional by a single adopted proposal.

---

### Likelihood Explanation

This requires a `ManageNetworkEconomics` proposal to be adopted by NNS governance majority. This is a privileged governance action, but it is reachable by any NNS neuron holder who can assemble a voting majority — including via liquid democracy (following). The NNS has a large number of neurons with significant stake that follow named neurons. A coordinated or malicious named neuron with sufficient following could pass such a proposal. The attack is also possible via a governance mistake (accidental zero-value submission). The `ManageNetworkEconomics` proposal type is a standard, publicly accessible NNS proposal type. [7](#0-6) 

---

### Recommendation

Add minimum lower-bound checks in `VotingPowerEconomics::validate()` for `start_reducing_voting_power_after_seconds` and `clear_following_after_seconds`. Both should be required to be at least some meaningful minimum (e.g., one day or one week) to prevent the decay window from collapsing to zero. The comment "They are allowed to be set to 0 though" should be removed and replaced with an enforced floor, analogous to how `INITIAL_VOTING_PERIOD_SECONDS_FLOOR` is enforced in SNS governance. [8](#0-7) 

Additionally, consider enforcing a relationship constraint: `start_reducing_voting_power_after_seconds` should be meaningfully larger than the maximum NNS proposal voting period, so that active voters cannot lose their deciding voting power before a proposal they voted on is decided.

---

### Proof of Concept

1. An NNS neuron holder with sufficient voting power submits a `ManageNetworkEconomics` proposal with:
   ```
   voting_power_economics: Some(VotingPowerEconomics {
       start_reducing_voting_power_after_seconds: Some(0),
       clear_following_after_seconds: Some(0),
       neuron_minimum_dissolve_delay_to_vote_seconds: Some(14 * ONE_DAY_SECONDS),
   })
   ```
2. The proposal passes `validate_manage_network_economics` because `VotingPowerEconomics::validate()` only checks that fields are `Some`, not that they are nonzero. [9](#0-8) 
3. The proposal is adopted and `perform_manage_network_economics_impl` stores the new parameters. [10](#0-9) 
4. On the next periodic task, `deciding_voting_power_adjustment_factor_function` constructs the range `0..0`. The `LinearMap` over an empty range returns a value that, when clamped to `[0,1]`, yields `0` for all neurons. [3](#0-2) 
5. All neurons have deciding voting power = 0. All following is cleared. No future proposal can reach quorum. The NNS is governance-locked.

### Citations

**File:** rs/nns/governance/src/network_economics.rs (L35-42)
```rust
    pub fn apply_changes_and_validate(
        &self,
        changes: &NetworkEconomics,
    ) -> Result<Self, Vec<String>> {
        let result = changes.inherit_from(self);
        result.validate()?;
        Ok(result)
    }
```

**File:** rs/nns/governance/src/network_economics.rs (L324-336)
```rust
    fn deciding_voting_power_adjustment_factor_function(&self) -> LinearMap {
        let from_range = {
            let begin = self.get_start_reducing_voting_power_after_seconds();
            let end = begin.saturating_add(self.get_clear_following_after_seconds());

            begin..end
        };

        #[allow(clippy::reversed_empty_ranges)]
        let to_range = 1..0;

        LinearMap::new(from_range, to_range)
    }
```

**File:** rs/nns/governance/src/network_economics.rs (L348-403)
```rust
    /// This just validates that all fields are set.
    ///
    /// They are allowed to be set to 0 though.
    ///
    /// In practice, we would never see None in any fields, because
    /// ManageNetworkEconomics has no way to set fields to None (see impl
    /// InheritFrom for Option), and in production, these fields are already set
    /// to Some.
    ///
    /// If Err is returned, it will be a nonempty Vec of defects.
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

**File:** rs/nns/governance/src/neuron/types.rs (L528-558)
```rust
    pub(crate) fn prune_following(
        &mut self,
        voting_power_economics: &VotingPowerEconomics,
        now_seconds: u64,
    ) -> u64 {
        let is_fresh = self.voting_power_refreshed_timestamp_seconds
            >= now_seconds
                .saturating_sub(
                    voting_power_economics.get_start_reducing_voting_power_after_seconds(),
                )
                .saturating_sub(voting_power_economics.get_clear_following_after_seconds());
        if is_fresh {
            return 0;
        }

        let mut result = 0_usize;
        for (topic, followees) in &self.followees {
            if *topic == Topic::NeuronManagement as i32 {
                continue;
            }
            result = result.saturating_add(followees.followees.len());
        }

        // Clear all following except ManageNeuron.
        self.followees
            .retain(|topic, _| *topic == Topic::NeuronManagement as i32);

        // If this panics, that means we somehow have around 2^64 (or more)
        // followees, which is not only disallowed, but just way more than we
        // would ever be able to hold in memory.
        u64::try_from(result).unwrap()
```

**File:** rs/nns/governance/src/governance.rs (L4288-4318)
```rust
    fn perform_manage_network_economics(
        &mut self,
        proposal_id: u64,
        proposed_network_economics: NetworkEconomics,
    ) {
        let result = self.perform_manage_network_economics_impl(proposed_network_economics);
        self.set_proposal_execution_status::<()>(proposal_id, result.map(|()| vec![]));
    }

    /// Only call this from perform_manage_network_economics.
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
