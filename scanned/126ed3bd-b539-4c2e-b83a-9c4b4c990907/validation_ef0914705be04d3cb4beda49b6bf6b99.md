### Title
Missing Inter-Parameter Validation in `VotingPowerEconomics::validate` Allows Zero-Value `start_reducing_voting_power_after_seconds` and `clear_following_after_seconds` to Break Voting Power Decay Logic - (File: rs/nns/governance/src/network_economics.rs)

---

### Summary

The `VotingPowerEconomics::validate` function in the NNS Governance canister explicitly states that `start_reducing_voting_power_after_seconds` and `clear_following_after_seconds` **"are allowed to be set to 0"**, but setting either to zero produces a degenerate `LinearMap` range (`begin..begin` or `begin..begin+0`) that causes `deciding_voting_power_adjustment_factor` to return 0 for all neurons immediately — effectively stripping all deciding voting power from every NNS neuron on the network. A `ManageNetworkEconomics` NNS proposal (requiring only a governance majority, not a privileged key) can set these values to 1 (the minimum non-zero value that `InheritFrom` will not treat as "unchanged"), causing the voting power decay window to collapse to 1 second and immediately zeroing out deciding voting power for all neurons.

---

### Finding Description

`VotingPowerEconomics::validate` only checks that `start_reducing_voting_power_after_seconds` and `clear_following_after_seconds` are `Some(_)` — it does not enforce any lower bound on their values:

```rust
// rs/nns/governance/src/network_economics.rs:348-403
/// This just validates that all fields are set.
///
/// They are allowed to be set to 0 though.
pub fn validate(&self) -> Result<(), Vec<String>> {
    let mut defects = vec![];

    if self.start_reducing_voting_power_after_seconds.is_none() {
        defects.push("start_reducing_voting_power_after_seconds must be set.".to_string());
    }

    if self.clear_following_after_seconds.is_none() {
        defects.push("clear_following_after_seconds must be set.".to_string());
    }
    // ... only neuron_minimum_dissolve_delay_to_vote_seconds has a range check
``` [1](#0-0) 

The `deciding_voting_power_adjustment_factor_function` builds a `LinearMap` using these two values:

```rust
fn deciding_voting_power_adjustment_factor_function(&self) -> LinearMap {
    let from_range = {
        let begin = self.get_start_reducing_voting_power_after_seconds();
        let end = begin.saturating_add(self.get_clear_following_after_seconds());
        begin..end
    };
    let to_range = 1..0;
    LinearMap::new(from_range, to_range)
}
``` [2](#0-1) 

If `start_reducing_voting_power_after_seconds = 1` and `clear_following_after_seconds = 1`, then `from_range = 1..2`. Any neuron that has not refreshed within the last 1 second (i.e., virtually every neuron) immediately has its deciding voting power reduced to 0. The result is clamped to `[0, 1]`:

```rust
pub fn deciding_voting_power_adjustment_factor(...) -> Decimal {
    self.deciding_voting_power_adjustment_factor_function()
        .apply(time_since_last_voting_power_refreshed.as_secs())
        .clamp(Decimal::from(0), Decimal::from(1))
}
``` [3](#0-2) 

The `InheritFrom` implementation for `u64` treats `0` as "unchanged" (inherits from base), so a proposal must use value `1` (not `0`) to actually set these fields to a near-zero value:

```rust
impl InheritFrom for u64 {
    fn inherit_from(&self, base: &Self) -> Self {
        if self == &0_u64 {
            return *base;
        }
        *self
    }
}
``` [4](#0-3) 

This means a `ManageNetworkEconomics` proposal with `start_reducing_voting_power_after_seconds = 1` and `clear_following_after_seconds = 1` passes all validation and, once executed, causes `deciding_voting_power_adjustment_factor` to return 0 for every neuron that has not refreshed within the last 2 seconds — which is effectively all neurons.

The deciding voting power is used directly in proposal voting:

```rust
let adjustment_factor: Decimal = {
    let time_since_last_refreshed = Duration::from_secs(
        now_seconds.saturating_sub(self.voting_power_refreshed_timestamp_seconds),
    );
    voting_power_economics
        .deciding_voting_power_adjustment_factor(time_since_last_refreshed)
};
let deciding_voting_power = adjustment_factor * potential_voting_power.floor();
``` [5](#0-4) 

With deciding voting power zeroed for all neurons, no proposal can reach quorum, and the NNS governance system is effectively frozen.

---

### Impact Explanation

Setting `start_reducing_voting_power_after_seconds = 1` and `clear_following_after_seconds = 1` via a `ManageNetworkEconomics` proposal causes the deciding voting power of every NNS neuron to immediately collapse to 0 (since no neuron can have refreshed within the last 1 second at the moment of the next heartbeat/proposal). This means:

1. No future NNS proposal can reach quorum — governance is frozen.
2. The NNS cannot self-recover via governance proposals (since all deciding voting power is 0).
3. Node provider rewards, subnet upgrades, and all other NNS-controlled operations are blocked.

This is a governance authorization/parameter validation bug with protocol-level impact on the NNS.

---

### Likelihood Explanation

Exploiting this requires passing a `ManageNetworkEconomics` NNS proposal with a governance majority. This is a privileged operation — it requires controlling enough voting power to pass a proposal. However, the vulnerability class is directly analogous to M-11: a configuration setter that lacks inter-parameter or lower-bound validation, allowing values that break core protocol logic. The comment in the code itself acknowledges the gap: *"They are allowed to be set to 0 though"* — but the `InheritFrom` mechanism means `1` (not `0`) is the actual attack value, and `1` is not blocked by any check.

The likelihood is **medium**: it requires a governance majority, but the NNS has had contentious governance situations, and the missing validation is a clear design gap that could be triggered by a mistake or a malicious actor with sufficient voting power.

---

### Recommendation

Add lower-bound validation for `start_reducing_voting_power_after_seconds` and `clear_following_after_seconds` in `VotingPowerEconomics::validate`. Reasonable minimums (e.g., at least 1 day or 1 week) should be enforced to prevent the decay window from collapsing to a degenerate range. For example:

```rust
const MIN_START_REDUCING_VOTING_POWER_AFTER_SECONDS: u64 = ONE_DAY_SECONDS;
const MIN_CLEAR_FOLLOWING_AFTER_SECONDS: u64 = ONE_DAY_SECONDS;

if let Some(v) = self.start_reducing_voting_power_after_seconds {
    if v < MIN_START_REDUCING_VOTING_POWER_AFTER_SECONDS {
        defects.push(format!(
            "start_reducing_voting_power_after_seconds ({v}) must be at least {}.",
            MIN_START_REDUCING_VOTING_POWER_AFTER_SECONDS
        ));
    }
}
if let Some(v) = self.clear_following_after_seconds {
    if v < MIN_CLEAR_FOLLOWING_AFTER_SECONDS {
        defects.push(format!(
            "clear_following_after_seconds ({v}) must be at least {}.",
            MIN_CLEAR_FOLLOWING_AFTER_SECONDS
        ));
    }
}
```

---

### Proof of Concept

**Attacker-controlled entry path:** Submit a `ManageNetworkEconomics` NNS proposal (via `manage_neuron` → `MakeProposal` → `ManageNetworkEconomics`) with:

```
NetworkEconomics {
    voting_power_economics: Some(VotingPowerEconomics {
        start_reducing_voting_power_after_seconds: Some(1),
        clear_following_after_seconds: Some(1),
        neuron_minimum_dissolve_delay_to_vote_seconds: Some(<valid value>),
    }),
    // all other fields = 0 (treated as "unchanged" by InheritFrom)
    ..Default::default()
}
```

**Validation path:** `validate_manage_network_economics` → `apply_changes_and_validate` → `VotingPowerEconomics::validate` — passes, because only `None`-ness is checked for these two fields. [6](#0-5) 

**Effect after proposal execution:** `perform_manage_network_economics_impl` stores the new `NetworkEconomics`. On the next call to `potential_and_deciding_voting_power` for any neuron, `deciding_voting_power_adjustment_factor` returns 0 (since `time_since_last_refreshed` ≥ 2 seconds for all neurons), zeroing deciding voting power network-wide. [7](#0-6) [2](#0-1)

### Citations

**File:** rs/nns/governance/src/network_economics.rs (L315-322)
```rust
    pub fn deciding_voting_power_adjustment_factor(
        &self,
        time_since_last_voting_power_refreshed: Duration,
    ) -> Decimal {
        self.deciding_voting_power_adjustment_factor_function()
            .apply(time_since_last_voting_power_refreshed.as_secs())
            .clamp(Decimal::from(0), Decimal::from(1))
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

**File:** rs/nns/governance/src/neuron/types.rs (L390-399)
```rust
        let adjustment_factor: Decimal = {
            let time_since_last_refreshed = Duration::from_secs(
                now_seconds.saturating_sub(self.voting_power_refreshed_timestamp_seconds),
            );

            voting_power_economics
                .deciding_voting_power_adjustment_factor(time_since_last_refreshed)
        };

        let deciding_voting_power = adjustment_factor * potential_voting_power.floor();
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
