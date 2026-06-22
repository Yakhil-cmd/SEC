### Title
`ManageNetworkEconomics` Proposals Cannot Set Parameters to Zero Due to Zero-as-Sentinel Convention - (File: `rs/nns/governance/src/network_economics.rs`)

### Summary

The NNS Governance canister's `ManageNetworkEconomics` proposal mechanism uses a "zero means unchanged" sentinel convention via the `InheritFrom` trait. This makes it impossible for any governance proposal to intentionally set `NetworkEconomics` parameters — such as `reject_cost_e8s`, `transaction_fee_e8s`, `neuron_minimum_stake_e8s`, or `Percentage`-typed fields — to zero, even when a legitimate NNS majority votes to do so. The adopted proposal silently has no effect, undermining governance integrity.

### Finding Description

The `InheritFrom` trait in `rs/nns/governance/src/network_economics.rs` is the mechanism by which a `ManageNetworkEconomics` proposal payload is merged with the current `NetworkEconomics` state. For every `u64` field, the implementation is:

```rust
impl InheritFrom for u64 {
    fn inherit_from(&self, base: &Self) -> Self {
        if self == &0_u64 {
            return *base;  // zero is treated as "no change"
        }
        *self
    }
}
```

The same sentinel pattern applies to `u32`, `Percentage { basis_points: Some(0) }`, and `Decimal { human_readable: Some("0") }`:

```rust
impl InheritFrom for Percentage {
    fn inherit_from(&self, base: &Self) -> Self {
        if self == &(Percentage { basis_points: Some(0) }) {
            return *base;  // 0% is treated as "no change"
        }
        *self
    }
}
```

This is applied recursively across the entire `NetworkEconomics` hierarchy — including `NeuronsFundEconomics` and `VotingPowerEconomics` — via `apply_changes_and_validate`:

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

The documentation explicitly acknowledges this design constraint: *"default values (0) are considered unchanged, so a valid proposal only needs to set the parameters that it wishes to change. In other words, it's not possible to set any of the values of NetworkEconomics to 0."*

The affected fields include:
- `reject_cost_e8s` (proposal rejection fee)
- `neuron_minimum_stake_e8s` (minimum neuron stake)
- `neuron_management_fee_per_proposal_e8s`
- `minimum_icp_xdr_rate`
- `neuron_spawn_dissolve_delay_seconds`
- `maximum_node_provider_rewards_e8s`
- `transaction_fee_e8s`
- `NeuronsFundEconomics::minimum_icp_xdr_rate` (a `Percentage`)
- `VotingPowerEconomics::start_reducing_voting_power_after_seconds` (via `Option<u64>` → `Some(0)` → inherits base)
- `VotingPowerEconomics::clear_following_after_seconds` (same)

The root cause is identical to the external report: **zero is conflated with "uninitialized / no change"**, making it structurally impossible to express an intentional zero value through the governance proposal interface.

### Impact Explanation

If the NNS community passes a `ManageNetworkEconomics` proposal intending to set any of the above parameters to zero (e.g., `reject_cost_e8s = 0` to eliminate proposal fees, `transaction_fee_e8s = 0` to eliminate ledger fees, or `minimum_icp_xdr_rate = 0%` to remove the ICP/XDR floor), the proposal will be adopted on-chain but will have **no effect** — the value silently remains at its current non-zero value. The governance system provides no error or rejection; the proposal status is `Executed`. This silently violates the expressed will of the NNS majority, undermining governance integrity for the entire Internet Computer network.

### Likelihood Explanation

Any NNS neuron holder with sufficient stake and dissolve delay can submit a `ManageNetworkEconomics` proposal. The scenario requires a legitimate NNS majority to vote for setting a parameter to zero — unusual but not impossible (e.g., a proposal to eliminate proposal fees during a bootstrapping phase, or to set `transaction_fee_e8s = 0` for a period). The silent failure mode (no error, `Executed` status) makes this particularly insidious: the community would not immediately know their decision was not applied.

### Recommendation

Replace the bare `u64`/`u32` fields in `NetworkEconomics` with `Option<u64>`/`Option<u32>`, using `None` as the sentinel for "no change" and `Some(0)` as an explicit zero. The `InheritFrom` implementation for `Option<T>` already correctly handles `None` as "inherit from base":

```rust
impl<T> InheritFrom for Option<T>
where T: InheritFrom + Clone,
{
    fn inherit_from(&self, base: &Self) -> Self {
        match (self, base) {
            (Some(me), Some(base)) => Some(me.inherit_from(base)),
            (Some(_), None) => self.clone(),
            (None, base) => base.clone(),  // None = "no change"
        }
    }
}
```

With `Option<u64>` fields, `Some(0)` would correctly propagate as an intentional zero, while `None` would inherit the existing value. The Candid interface and protobuf definitions would need corresponding updates to use `optional uint64` instead of `uint64`.

### Proof of Concept

1. Current `NetworkEconomics` state: `reject_cost_e8s = 100_000_000` (1 ICP).
2. NNS community submits and adopts a `ManageNetworkEconomics` proposal with `reject_cost_e8s: 0, ..Default::default()`.
3. `perform_manage_network_economics_impl` calls `apply_changes_and_validate(&proposed)`.
4. Inside `inherit_from`: `0_u64.inherit_from(&100_000_000_u64)` returns `100_000_000` (base value).
5. `self.heap_data.economics = Some(new_network_economics)` — the value is unchanged.
6. Proposal status is set to `Executed` with no error.
7. `reject_cost_e8s` remains `100_000_000` despite the adopted governance decision. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

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

**File:** rs/nns/governance/src/network_economics.rs (L410-430)
```rust
trait InheritFrom {
    /// Returns a modified copy of self where fields containing 0 are replaced
    /// with the value from base.
    fn inherit_from(&self, base: &Self) -> Self;
}

// Ideally, we'd use num_traits::Zero to give a generic implementation that
// applies to all integer types, but Rust refuses to allow that in the presence
// of impl InheritFrom for Option<T> below. If only there was some way we could
// tell Rust, "ok, but this impl does not apply when the type happens to be
// Option", but that doesn't exist (yet). Fortunately, we do not use a wide
// range of integer types. Therefore, only this one is needed (for now).
impl InheritFrom for u64 {
    fn inherit_from(&self, base: &Self) -> Self {
        if self == &0_u64 {
            return *base;
        }

        *self
    }
}
```

**File:** rs/nns/governance/src/network_economics.rs (L456-468)
```rust
impl InheritFrom for Percentage {
    fn inherit_from(&self, base: &Self) -> Self {
        if self
            == &(Percentage {
                basis_points: Some(0),
            })
        {
            return *base;
        }

        *self
    }
}
```

**File:** rs/nns/governance/src/network_economics.rs (L483-516)
```rust
impl InheritFrom for NetworkEconomics {
    fn inherit_from(&self, base: &Self) -> Self {
        Self {
            reject_cost_e8s: self.reject_cost_e8s.inherit_from(&base.reject_cost_e8s),
            neuron_minimum_stake_e8s: self
                .neuron_minimum_stake_e8s
                .inherit_from(&base.neuron_minimum_stake_e8s),
            neuron_management_fee_per_proposal_e8s: self
                .neuron_management_fee_per_proposal_e8s
                .inherit_from(&base.neuron_management_fee_per_proposal_e8s),
            minimum_icp_xdr_rate: self
                .minimum_icp_xdr_rate
                .inherit_from(&base.minimum_icp_xdr_rate),
            neuron_spawn_dissolve_delay_seconds: self
                .neuron_spawn_dissolve_delay_seconds
                .inherit_from(&base.neuron_spawn_dissolve_delay_seconds),
            maximum_node_provider_rewards_e8s: self
                .maximum_node_provider_rewards_e8s
                .inherit_from(&base.maximum_node_provider_rewards_e8s),
            transaction_fee_e8s: self
                .transaction_fee_e8s
                .inherit_from(&base.transaction_fee_e8s),
            max_proposals_to_keep_per_topic: self
                .max_proposals_to_keep_per_topic
                .inherit_from(&base.max_proposals_to_keep_per_topic),

            neurons_fund_economics: self
                .neurons_fund_economics
                .inherit_from(&base.neurons_fund_economics),
            voting_power_economics: self
                .voting_power_economics
                .inherit_from(&base.voting_power_economics),
        }
    }
```

**File:** rs/nns/governance/api/src/types.rs (L2098-2103)
```rust
/// Network economics contains the parameters for several operations related
/// to the economy of the network. When submitting a NetworkEconomics proposal
/// default values (0) are considered unchanged, so a valid proposal only needs
/// to set the parameters that it wishes to change.
/// In other words, it's not possible to set any of the values of
/// NetworkEconomics to 0.
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

**File:** rs/nns/governance/src/network_economics_tests.rs (L25-35)
```rust
            // This is equivalent to None, because 0 is ALWAYS vulnerable to
            // being overridden, even when inside Some. Therefore no change here.
            minimum_icp_xdr_rate: Some(Percentage {
                basis_points: Some(0),
            }),

            ..Default::default()
        }),

        // No change for these either.
        neuron_management_fee_per_proposal_e8s: 0,
```
