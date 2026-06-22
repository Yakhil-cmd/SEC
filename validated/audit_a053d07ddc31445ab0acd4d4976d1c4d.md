### Title
`VotingPowerEconomics.start_reducing_voting_power_after_seconds` and `clear_following_after_seconds` Have No Minimum Bound Validation, Allowing a Governance Proposal to Render NNS Governance Inoperable - (File: `rs/nns/governance/src/network_economics.rs`)

---

### Summary

The NNS `VotingPowerEconomics` struct contains two critical timing parameters — `start_reducing_voting_power_after_seconds` and `clear_following_after_seconds` — that can be set to arbitrarily small values (including `1`) via a `ManageNetworkEconomics` governance proposal. The validation logic in `VotingPowerEconomics::validate()` only checks that these fields are **set** (not `None`), but imposes **no minimum floor** on their values. Setting these to extremely small values causes all existing NNS neurons to immediately lose their deciding voting power and have their following cleared, making it impossible to pass any future governance proposals and effectively freezing the NNS.

---

### Finding Description

`VotingPowerEconomics` in the NNS governance canister contains two parameters:

- `start_reducing_voting_power_after_seconds`: After this many seconds without a voting power refresh, a neuron's deciding voting power begins decreasing linearly.
- `clear_following_after_seconds`: After this many additional seconds, deciding voting power reaches 0 and following is cleared.

These are settable via `ManageNetworkEconomics` proposals. The validation in `VotingPowerEconomics::validate()` is:

```rust
// rs/nns/governance/src/network_economics.rs lines 358-403
pub fn validate(&self) -> Result<(), Vec<String>> {
    let mut defects = vec![];

    if self.start_reducing_voting_power_after_seconds.is_none() {
        defects.push("start_reducing_voting_power_after_seconds must be set.".to_string());
    }

    if self.clear_following_after_seconds.is_none() {
        defects.push("clear_following_after_seconds must be set.".to_string());
    }
    // ... only neuron_minimum_dissolve_delay_to_vote_seconds has a range check
```

The code comment explicitly acknowledges: **"They are allowed to be set to 0 though."** [1](#0-0) 

Only `neuron_minimum_dissolve_delay_to_vote_seconds` has a range bound enforced (`NEURON_MINIMUM_DISSOLVE_DELAY_TO_VOTE_SECONDS_BOUNDS = 14 days..=6 months`). The other two fields have **no minimum floor**. [2](#0-1) 

The `InheritFrom` mechanism for `u64` treats `0` as "unchanged" (inherits from base), so a proposer cannot set these to `0` via a proposal — but they **can** set them to `1` (one second), which passes validation and is applied. [3](#0-2) 

When `start_reducing_voting_power_after_seconds = 1` and `clear_following_after_seconds = 1`, the `deciding_voting_power_adjustment_factor_function` computes a linear decay window of `[1, 2)` seconds. Any neuron whose `voting_power_refreshed_timestamp_seconds` is more than 1 second in the past (i.e., every neuron in practice) will have deciding voting power of 0. [4](#0-3) 

The `compute_voting_power_snapshot_for_standard_proposal` in the NNS neuron store uses this deciding voting power for ballots. With all deciding voting power at 0, no proposal can reach quorum. [5](#0-4) 

Additionally, `prune_some_following` uses these parameters to clear following on all neurons, destroying the delegation graph.

---

### Impact Explanation

**Impact: High** — If a `ManageNetworkEconomics` proposal sets `start_reducing_voting_power_after_seconds` to a very small value (e.g., `1`), all NNS neurons immediately appear "stale." Their deciding voting power drops to 0. No future proposal can achieve quorum. The NNS governance canister becomes unable to pass any proposals, including proposals to fix the parameter. The only recovery path would be a canister upgrade deployed by DFINITY via a hotfix mechanism outside normal governance — a severe operational disruption to the entire Internet Computer.

The `prune_some_following` background task would also clear all neuron following relationships (except `NeuronManagement`), destroying the delegation graph that most neurons rely on for participation.

---

### Likelihood Explanation

**Likelihood: Low** — A `ManageNetworkEconomics` proposal requires NNS governance majority to pass. This is a privileged governance action. However, the analog report classifies this as low likelihood / high impact, matching exactly: the parameter has a default value and is unlikely to be revised, but if it is revised to a pathological value, the impact is catastrophic. A governance participant with sufficient voting power (or a coalition) could accidentally or maliciously submit such a proposal. The lack of any minimum bound means there is no on-chain protection against this.

---

### Recommendation

Add minimum floor validation for `start_reducing_voting_power_after_seconds` and `clear_following_after_seconds` in `VotingPowerEconomics::validate()`, analogous to the existing `NEURON_MINIMUM_DISSOLVE_DELAY_TO_VOTE_SECONDS_BOUNDS` range check. Reasonable minimums would be on the order of days or weeks (e.g., 1 day minimum for each), preventing any proposal from setting these to values that would immediately zero out all neuron voting power. [6](#0-5) 

---

### Proof of Concept

1. A governance participant with sufficient NNS voting power submits a `ManageNetworkEconomics` proposal:

```
NetworkEconomics {
    voting_power_economics: Some(VotingPowerEconomics {
        start_reducing_voting_power_after_seconds: Some(1),  // 1 second
        clear_following_after_seconds: Some(1),              // 1 second
        neuron_minimum_dissolve_delay_to_vote_seconds: Some(14 * 86400), // valid
    }),
    ..Default::default()
}
```

2. The proposal passes `validate_manage_network_economics` because `VotingPowerEconomics::validate()` only checks that the fields are `Some(...)`, not that they are within a safe range. [7](#0-6) 

3. The proposal is executed via `perform_manage_network_economics_impl`, which calls `apply_changes_and_validate` — again passing validation. [8](#0-7) 

4. From this point forward, `deciding_voting_power_adjustment_factor` returns 0 for every neuron (since all neurons were last refreshed more than 1 second ago). The `compute_voting_power_snapshot_for_standard_proposal` assigns 0 deciding voting power to all neurons. [9](#0-8) 

5. No future proposal can achieve quorum. The NNS is effectively frozen. The `prune_some_following` background task clears all following relationships, compounding the damage.

### Citations

**File:** rs/nns/governance/src/network_economics.rs (L293-294)
```rust
    pub const NEURON_MINIMUM_DISSOLVE_DELAY_TO_VOTE_SECONDS_BOUNDS: RangeInclusive<u64> =
        (14 * ONE_DAY_SECONDS)..=(6 * ONE_MONTH_SECONDS);
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

**File:** rs/nns/governance/src/network_economics.rs (L422-430)
```rust
impl InheritFrom for u64 {
    fn inherit_from(&self, base: &Self) -> Self {
        if self == &0_u64 {
            return *base;
        }

        *self
    }
}
```

**File:** rs/nns/governance/src/neuron_store/voting_power.rs (L144-160)
```rust
        let mut process_neuron = |neuron: &Neuron| {
            if neuron.is_inactive(now_seconds)
                || neuron.dissolve_delay_seconds(now_seconds) < min_dissolve_delay_seconds
            {
                return;
            }

            let (potential_voting_power, deciding_voting_power) =
                neuron.potential_and_deciding_voting_power(voting_power_economics, now_seconds);
            // We don't handle overflow here, as in `get_voting_power_as_u64` below,
            // the input arguments bigger than u64::MAX will result in an error.
            total_deciding_voting_power =
                total_deciding_voting_power.saturating_add(deciding_voting_power as u128);
            total_potential_voting_power =
                total_potential_voting_power.saturating_add(potential_voting_power as u128);
            voting_power_map.insert(neuron.id().id, deciding_voting_power);
        };
```

**File:** rs/nns/governance/src/governance.rs (L4298-4317)
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
```

**File:** rs/nns/governance/src/governance.rs (L4722-4745)
```rust
    fn validate_manage_network_economics(
        &self,
        // (Note that this is the value associated with ManageNetworkEconomics,
        // not the resulting NetworkEconomics.)
        proposed_network_economics: &NetworkEconomics,
    ) -> Result<(), GovernanceError> {
        // It maybe does not make sense to be able to set transaction_fee_e8s
        // via proposal. What we probably want instead is to fetch this value
        // from ledger.

        self.economics()
            .apply_changes_and_validate(proposed_network_economics)
            .map_err(|defects| {
                let message = format!(
                    "The resulting settings would not be valid for the \
                     following reason(s):\n\
                     - {}",
                    defects.join("\n  - "),
                );

                GovernanceError::new_with_message(ErrorType::InvalidProposal, message)
            })?;

        Ok(())
```
