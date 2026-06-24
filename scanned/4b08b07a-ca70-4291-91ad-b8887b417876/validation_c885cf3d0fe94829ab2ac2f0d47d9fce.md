### Title
Inconsistent Voting Power Returned by `NeuronInfo.voting_power` vs `NeuronInfo.deciding_voting_power` — (`rs/nns/governance/src/neuron/types.rs`)

---

### Summary

The deprecated `voting_power` field in `NeuronInfo` is documented in the DID interface as having "the same value as `deciding_voting_power`", but the implementation sets it to `potential_voting_power` instead. This means any external protocol or off-chain component that reads the `voting_power` field from `get_neuron_info` or `get_neuron_info_by_id_or_subaccount` will receive an inflated, refresh-penalty-free value for neurons that have not refreshed their voting power recently — directly contradicting the documented contract.

---

### Finding Description

In `rs/nns/governance/src/neuron/types.rs`, the `get_neuron_info` method constructs a `NeuronInfo` struct. It correctly computes both `potential_voting_power` and `deciding_voting_power` via `potential_and_deciding_voting_power`, but then assigns the deprecated `voting_power` field to `potential_voting_power`:

```rust
NeuronInfo {
    deciding_voting_power: Some(deciding_voting_power),
    potential_voting_power: Some(potential_voting_power),
    voting_power: potential_voting_power,   // <-- BUG: should be deciding_voting_power
    ...
}
``` [1](#0-0) 

The DID interface explicitly documents the `voting_power` field in `NeuronInfo` as:

> *"Deprecated. Use either deciding_voting_power or potential_voting_power instead. **Has the same value as deciding_voting_power.**"* [2](#0-1) 

The distinction between the two values is significant. `deciding_voting_power` applies a linear reduction factor when a neuron has not refreshed its voting power for more than `start_reducing_voting_power_after_seconds` (currently 6 months), eventually reaching 0 after `clear_following_after_seconds` (currently 1 month more). `potential_voting_power` ignores this penalty entirely. [3](#0-2) 

The `get_neuron_info` method is explicitly documented as not requiring authorization — any unprivileged caller can invoke it: [4](#0-3) 

The same inconsistency is present in `get_neuron_info_by_id_or_subaccount`, which also calls `get_neuron_info` internally: [5](#0-4) 

---

### Impact Explanation

External protocols, governance dashboards, or off-chain tallying systems that consume the `voting_power` field from `NeuronInfo` (which the DID says equals `deciding_voting_power`) will instead receive `potential_voting_power`. For neurons that have not refreshed their voting power recently, `potential_voting_power` can be significantly higher than `deciding_voting_power` — up to 100% higher (when `deciding_voting_power` has been reduced to 0 but `potential_voting_power` remains at full value). This causes:

1. **Inflated voting power reporting**: Off-chain governance tallying systems or external protocols integrating with NNS governance will overestimate the effective voting power of stale neurons.
2. **Inconsistent API contract**: The DID-documented invariant (`voting_power == deciding_voting_power`) is violated, breaking any consumer that relies on it.

The on-chain ballot computation itself is unaffected — it uses `deciding_voting_power` directly via `compute_voting_power_snapshot_for_standard_proposal`. [6](#0-5) 

---

### Likelihood Explanation

Medium. The `voting_power` field is deprecated, and the DID encourages callers to use `deciding_voting_power` or `potential_voting_power` directly. However, the field is still present in the live API, still populated, and its documented contract ("has the same value as `deciding_voting_power`") is actively wrong. Any existing integration built before `deciding_voting_power` was introduced, or any integration that trusts the DID documentation, will silently receive incorrect data. The discrepancy only manifests for neurons that have not refreshed in >6 months, which is an increasingly common state as the refresh-penalty mechanism (Mission 70) takes effect.

---

### Recommendation

Change line 960 of `rs/nns/governance/src/neuron/types.rs` to assign `deciding_voting_power` to the deprecated `voting_power` field, consistent with the DID documentation:

```rust
voting_power: deciding_voting_power,  // matches documented contract
``` [7](#0-6) 

---

### Proof of Concept

1. Create a neuron with a dissolve delay ≥ 6 months and stake it.
2. Do not refresh voting power for >6 months (do not vote directly, do not set following, do not call `refresh_voting_power`).
3. Call `get_neuron_info` on the NNS governance canister for that neuron.
4. Observe that `neuron_info.voting_power == neuron_info.potential_voting_power` (non-zero), while `neuron_info.deciding_voting_power` is 0 (or significantly reduced).
5. The DID contract states `voting_power` should equal `deciding_voting_power`, but it equals `potential_voting_power` instead — a direct contradiction confirmed by the test at line 211 of `rs/nns/governance/src/neuron/types/tests.rs`:

```rust
voting_power: potential_voting_power,  // test confirms the bug
``` [8](#0-7)

### Citations

**File:** rs/nns/governance/src/neuron/types.rs (L389-399)
```rust
        // For DECIDING voting power.
        let adjustment_factor: Decimal = {
            let time_since_last_refreshed = Duration::from_secs(
                now_seconds.saturating_sub(self.voting_power_refreshed_timestamp_seconds),
            );

            voting_power_economics
                .deciding_voting_power_adjustment_factor(time_since_last_refreshed)
        };

        let deciding_voting_power = adjustment_factor * potential_voting_power.floor();
```

**File:** rs/nns/governance/src/neuron/types.rs (L958-963)
```rust
            deciding_voting_power: Some(deciding_voting_power),
            potential_voting_power: Some(potential_voting_power),
            voting_power: potential_voting_power,
            eight_year_gang_bonus_base_e8s: Some(self.eight_year_gang_bonus_base_e8s),
            staked_maturity_e8s_equivalent: self.staked_maturity_e8s_equivalent,
        }
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

**File:** rs/nns/governance/src/governance.rs (L3261-3273)
```rust
    /// Returns the neuron info for a given neuron `id`. This method
    /// does not require authorization, so the `NeuronInfo` of a
    /// neuron is accessible to any caller.
    pub fn get_neuron_info(
        &self,
        id: &NeuronId,
        requester: PrincipalId,
    ) -> Result<NeuronInfo, GovernanceError> {
        let now = self.env.now();
        self.with_neuron(id, |neuron| {
            neuron.get_neuron_info(self.voting_power_economics(), now, requester, false)
        })
    }
```

**File:** rs/nns/governance/src/governance.rs (L3314-3327)
```rust
    pub fn get_neuron_info_by_id_or_subaccount(
        &self,
        find_by: &NeuronIdOrSubaccount,
        requester: PrincipalId,
    ) -> Result<NeuronInfo, GovernanceError> {
        self.with_neuron_by_neuron_id_or_subaccount(find_by, |neuron| {
            neuron.get_neuron_info(
                self.voting_power_economics(),
                self.env.now(),
                requester,
                false,
            )
        })
    }
```

**File:** rs/nns/governance/src/neuron_store/voting_power.rs (L151-159)
```rust
            let (potential_voting_power, deciding_voting_power) =
                neuron.potential_and_deciding_voting_power(voting_power_economics, now_seconds);
            // We don't handle overflow here, as in `get_voting_power_as_u64` below,
            // the input arguments bigger than u64::MAX will result in an error.
            total_deciding_voting_power =
                total_deciding_voting_power.saturating_add(deciding_voting_power as u128);
            total_potential_voting_power =
                total_potential_voting_power.saturating_add(potential_voting_power as u128);
            voting_power_map.insert(neuron.id().id, deciding_voting_power);
```

**File:** rs/nns/governance/src/neuron/types/tests.rs (L211-211)
```rust
            voting_power: potential_voting_power,
```
