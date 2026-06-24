### Title
`NeuronInfo.voting_power` Returns `potential_voting_power` Instead of `deciding_voting_power` in `get_neuron_info` — (File: `rs/nns/governance/src/neuron/types.rs`)

---

### Summary

The `get_neuron_info` function in the NNS Governance canister sets the `voting_power` field of `NeuronInfo` to `potential_voting_power` instead of `deciding_voting_power`. The DID interface explicitly documents that `voting_power` "Has the same value as `deciding_voting_power`." For neurons that have not refreshed their voting power in over 6 months, `deciding_voting_power` is reduced (linearly to 0 over 1 month), while `potential_voting_power` remains at the full value. Any unprivileged query caller reading `voting_power` from `NeuronInfo` receives an inflated, incorrect value that contradicts the specification.

---

### Finding Description

In `rs/nns/governance/src/neuron/types.rs`, the `get_neuron_info` method correctly computes both `potential_voting_power` and `deciding_voting_power` via `potential_and_deciding_voting_power`:

```rust
let (potential_voting_power, deciding_voting_power) =
    self.potential_and_deciding_voting_power(voting_power_economics, now_seconds);
```

However, when constructing the returned `NeuronInfo`, the deprecated `voting_power` field is set to `potential_voting_power` rather than `deciding_voting_power`:

```rust
NeuronInfo {
    ...
    deciding_voting_power: Some(deciding_voting_power),
    potential_voting_power: Some(potential_voting_power),
    voting_power: potential_voting_power,   // <-- BUG: should be deciding_voting_power
    ...
}
```

The DID interface at `rs/nns/governance/canister/governance.did` lines 910–917 explicitly states:

```
// Deprecated. Use either deciding_voting_power or potential_voting_power
// instead. Has the same value as deciding_voting_power.
```

The `deciding_voting_power` is computed by applying a time-based adjustment factor from `VotingPowerEconomics.deciding_voting_power_adjustment_factor`. For a neuron that has not refreshed in more than `start_reducing_voting_power_after_seconds` (default: 6 months), this factor decreases linearly from 1 to 0 over `clear_following_after_seconds` (default: 1 month). After 7 months without a refresh, `deciding_voting_power` reaches 0, while `potential_voting_power` remains at the full staked value. The `voting_power` field in `NeuronInfo` therefore returns the full (inflated) value instead of 0.

The `deciding_voting_power` is the value actually used for:
1. Ballot weights when proposals are created (`compute_ballots_for_standard_proposal`)
2. Voting reward shares for neurons voting via following

---

### Impact Explanation

Any unprivileged query caller invoking `get_neuron_info` or `get_neuron_info_by_id_or_subaccount` on the NNS Governance canister receives a `voting_power` value that is inflated relative to the neuron's actual governance participation. For a neuron that has not refreshed in ≥7 months, `deciding_voting_power = 0` but `voting_power` (as returned) equals the full `potential_voting_power`. Users and tooling that rely on the `voting_power` field (which the DID spec says equals `deciding_voting_power`) will:

- Incorrectly believe the neuron has active voting power and is earning voting rewards
- Fail to recognize the need to refresh voting power to restore governance participation
- Receive misleading reward estimates, directly analogous to the external report's `getRewardForStakingStore` returning a stale share percentage

---

### Likelihood Explanation

The condition is reachable by any neuron holder who has not voted directly, set following, or called `RefreshVotingPower` in over 6 months — a realistic scenario for passive stakers. The discrepancy is observable by any unprivileged query caller with no special permissions. The `voting_power` field is the historically primary field for voting power in `NeuronInfo` and is widely used by dashboards and tooling.

---

### Recommendation

Update `get_neuron_info` in `rs/nns/governance/src/neuron/types.rs` to set the deprecated `voting_power` field to `deciding_voting_power`, consistent with the DID specification:

```rust
voting_power: deciding_voting_power,
```

---

### Proof of Concept

1. Create or identify a neuron whose `voting_power_refreshed_timestamp_seconds` is more than 7 months in the past (i.e., `now - voting_power_refreshed_timestamp_seconds > 7 * ONE_MONTH_SECONDS`).
2. Call `get_neuron_info` (a query endpoint, accessible to any unprivileged caller) for that neuron.
3. Observe that the returned `NeuronInfo.voting_power` equals `potential_voting_power` (non-zero), while `NeuronInfo.deciding_voting_power` equals 0.
4. Per the DID spec, `voting_power` should equal `deciding_voting_power` (0), but instead it returns the full staked value — an inflated, misleading result.

The root cause is confirmed at: [1](#0-0) 

The DID specification that is violated: [2](#0-1) 

The dynamic `deciding_voting_power` computation that should be used: [3](#0-2) 

The `VotingPowerEconomics.deciding_voting_power_adjustment_factor` that drives the time-based decay: [4](#0-3)

### Citations

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

**File:** rs/nns/governance/src/neuron/types.rs (L958-960)
```rust
            deciding_voting_power: Some(deciding_voting_power),
            potential_voting_power: Some(potential_voting_power),
            voting_power: potential_voting_power,
```

**File:** rs/nns/governance/canister/governance.did (L910-917)
```text
  // Deprecated. Use either deciding_voting_power or potential_voting_power
  // instead. Has the same value as deciding_voting_power.
  //
  // Previously, if a neuron had < 6 months dissolve delay (making it ineligible
  // to vote), this would not get set to 0 (zero). That was pretty confusing.
  // Now that this is set to deciding_voting_power, this actually does get
  // zeroed out.
  voting_power : nat64;
```

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
