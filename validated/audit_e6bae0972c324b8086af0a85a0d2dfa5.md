### Title
Stale `default_followees` Governance Parameter Has No Effect on Neuron Creation — (`File: rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto`)

---

### Summary

The `default_followees` field in `NervousSystemParameters` of the SNS Governance canister is explicitly documented as having **no effect**, yet it is exposed as a settable governance parameter via `ManageNervousSystemParameters` proposals. Any such proposal that touches this field will be executed and marked as `Executed` on-chain, but the stored value is never consulted during neuron creation. This is a direct analog to the `UniversalBuyback` stale-split-ratio bug: a setter updates persisted state, but the actual computation ignores the updated value.

---

### Finding Description

The `NervousSystemParameters` message in the SNS Governance canister contains a `default_followees` field. The proto definition and all generated Rust code carry an explicit comment:

> `// TODO NNS1-2169: This field currently has no effect.` [1](#0-0) [2](#0-1) 

Despite this, the field is part of the publicly-exposed `NervousSystemParameters` type that any SNS neuron holder can propose to change via a `ManageNervousSystemParameters` governance proposal. When such a proposal is adopted, `perform_manage_nervous_system_parameters` writes the new parameters (including `default_followees`) into `self.proto.parameters`: [3](#0-2) 

The validation layer enforces that `default_followees.followees` must be **empty** — it rejects any non-empty value: [4](#0-3) [5](#0-4) 

So the field can be "set" (to an empty map) via a successfully-executed governance proposal, but it is **never read** during neuron creation. The helper `default_followees_or_panic()` exists but carries the same TODO note and is not invoked in the neuron-claiming path: [6](#0-5) 

The net result mirrors the `UniversalBuyback` pattern exactly: a governance action updates persisted state, the proposal is marked `Executed`, but the underlying behavior (default followees assigned to new neurons) is unchanged.

---

### Impact Explanation

SNS community members and operators who inspect `NervousSystemParameters` see `default_followees` as a configurable governance parameter. They may:

1. Submit and pass a `ManageNervousSystemParameters` proposal believing it will configure default followees for all future neurons.
2. Observe the proposal marked `Executed` on-chain.
3. Discover that newly created neurons still have no default followees — the executed proposal had zero effect.

This creates a misleading governance record: the on-chain proposal history shows a parameter change was executed, but the SNS's actual neuron-following behavior is unaffected. For SNS communities that rely on default followees to bootstrap participation (e.g., ensuring new neurons follow a known neuron for critical proposals), this silent no-op can lead to governance participation failures.

---

### Likelihood Explanation

Any SNS neuron holder with sufficient voting power can submit a `ManageNervousSystemParameters` proposal. The entry path is fully unprivileged and reachable via the standard `manage_neuron` ingress call. The misleading behavior is triggered whenever such a proposal is adopted and executed, which is a normal governance operation.

---

### Recommendation

Either:
1. **Implement the feature**: Honor `default_followees` during neuron creation (i.e., assign the configured followees when `claim_or_refresh_neuron` creates a new neuron), removing the TODO.
2. **Remove the field**: If default followees are not planned, remove `default_followees` from `NervousSystemParameters` entirely so it cannot be set via governance proposals.
3. **Block proposals that set it**: Until the feature is implemented, reject `ManageNervousSystemParameters` proposals that include a `default_followees` field (even an empty one), to prevent misleading governance records.

---

### Proof of Concept

1. An SNS neuron holder submits a `ManageNervousSystemParameters` proposal with `default_followees: Some(DefaultFollowees { followees: {} })`.
2. The proposal passes validation (empty map is accepted by `validate_default_followees`).
3. The proposal is adopted and `perform_manage_nervous_system_parameters` writes the new parameters to state — the proposal is marked `Executed`.
4. A new neuron is subsequently claimed via `claim_or_refresh_neuron`. The neuron is created with no followees, because `default_followees` is never consulted in the neuron creation path.
5. The on-chain governance record shows a successfully executed parameter change, but the SNS behavior is identical to before the proposal. [1](#0-0) [3](#0-2) [4](#0-3)

### Citations

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L1164-1173)
```text
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
```

**File:** rs/sns/governance/src/gen/ic_sns_governance.pb.v1.rs (L1693-1703)
```rust
    /// TODO NNS1-2169: This field currently has no effect.
    /// TODO NNS1-2169: Design and implement this feature.
    ///
    /// The set of default followees that every newly created neuron will follow
    /// per function. This is specified as a mapping of proposal functions to followees.
    ///
    /// If unset, neurons will have no followees by default.
    /// The set of followees for each function can be at most of size
    /// max_followees_per_function.
    #[prost(message, optional, tag = "6")]
    pub default_followees: ::core::option::Option<DefaultFollowees>,
```

**File:** rs/sns/governance/src/governance.rs (L562-588)
```rust
/// Requires that the neurons identified in base.parameters.default_followees
/// exist (i.e. be in base.neurons).
///
/// Assumes that base.parameters is Some.
///
/// If the validation fails, an Err is returned containing a string that explains why
/// base is invalid.
///
/// TODO NNS1-2169: default followees are not currently supported.
pub fn validate_default_followees(base: &GovernanceProto) -> Result<(), String> {
    base.parameters
        .as_ref()
        .expect("GovernanceProto.parameters is not populated.")
        .default_followees
        .as_ref()
        .ok_or_else(|| "GovernanceProto.parameters.default_followees must be set".to_string())
        .and_then(|default_followees| {
            if default_followees.followees.is_empty() {
                Ok(())
            } else {
                Err(format!(
                    "DefaultFollowees.default_followees must be empty, but found {:?}",
                    default_followees.followees
                ))
            }
        })
}
```

**File:** rs/sns/governance/src/governance.rs (L2581-2597)
```rust
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
```

**File:** rs/sns/governance/src/governance.rs (L3357-3366)
```rust
    /// Returns the default followees that a newly claimed neuron will have, as defined in
    /// the nervous system parameters' default_followees.
    /// TODO NNS1-2169: default followees are not currently supported.
    fn default_followees_or_panic(&self) -> DefaultFollowees {
        self.nervous_system_parameters_or_panic()
            .default_followees
            .as_ref()
            .expect("NervousSystemParameters.default_followees must be present")
            .clone()
    }
```

**File:** rs/sns/governance/src/types.rs (L716-732)
```rust
    /// Validates that the nervous system parameter default_followees is well-formed.
    /// TODO NNS1-2169: default followees are not currently supported
    fn validate_default_followees(&self) -> Result<(), String> {
        self.default_followees
            .as_ref()
            .ok_or_else(|| "NervousSystemParameters.default_followees must be set".to_string())
            .and_then(|default_followees| {
                if default_followees.followees.is_empty() {
                    Ok(())
                } else {
                    Err(format!(
                        "DefaultFollowees.default_followees must be empty, but found {:?}",
                        default_followees.followees
                    ))
                }
            })
    }
```
