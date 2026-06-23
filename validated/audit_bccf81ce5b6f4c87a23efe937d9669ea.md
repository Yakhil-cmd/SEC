### Title
SNS `NervousSystemParameters.default_followees` Has No Effect Despite Being Documented and Exposed in Public API — (`rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto`)

---

### Summary

The `default_followees` field in SNS `NervousSystemParameters` is documented as "The set of default followees that every newly created neuron will follow per function," is exposed in the public Candid API, and is required to be present — yet the implementation explicitly states it has **no effect**. This is a direct analog to the PrePO market expiry bug: a governance parameter is stored, validated, and documented to produce a specific behavioral outcome, but the implementation silently ignores it, causing actual behavior to diverge from documented behavior.

---

### Finding Description

The `NervousSystemParameters.default_followees` field is simultaneously:

1. **Documented** to configure default followees for every newly created neuron.
2. **Exposed** in the public Candid interface (`governance.did`).
3. **Required to be present** — validation fails if the field is absent.
4. **Required to be empty** — validation rejects any non-empty value.
5. **Explicitly acknowledged as having no effect** in multiple source locations.

The proto definition states:

> `// TODO NNS1-2169: This field currently has no effect.`
> `// TODO NNS1-2169: Design and implement this feature.`
> `// The set of default followees that every newly created neuron will follow per function.` [1](#0-0) 

The generated Rust struct carries the same TODO: [2](#0-1) 

The `validate_default_followees` function in `NervousSystemParameters` enforces that the field must be **empty**, making it impossible for any SNS community to configure non-empty default followees via a `ManageNervousSystemParameters` proposal: [3](#0-2) 

The same enforcement exists at the `GovernanceProto` level: [4](#0-3) 

The internal accessor also carries the "not currently supported" caveat: [5](#0-4) 

The field is fully exposed in the public Candid interface, giving SNS communities the false impression that it is a configurable, operative parameter: [6](#0-5) 

---

### Impact Explanation

Any SNS that is initialized or governed under the assumption that `default_followees` will be applied to newly claimed neurons will find that:

- New neurons are created with **zero followees**, regardless of what the SNS community has configured.
- Governance participation from new participants is lower than the SNS designers expected, because new neurons do not automatically delegate their voting power.
- Proposals can be decided with a smaller fraction of total voting power than the SNS community intended, weakening the governance security model.
- SNS communities that designed their tokenomics and governance thresholds around the assumption that new neurons would inherit default followees may find their governance is more easily captured or deadlocked.

This is structurally identical to the PrePO finding: a field is stored and documented to produce outcome X (default followees applied), but the implementation produces outcome Y (no followees applied), and the discrepancy is invisible to users of the public API.

---

### Likelihood Explanation

The likelihood is **high**:

- The field is part of the public Candid API and is required to be present in every SNS deployment.
- Any SNS developer reading the proto documentation or the Candid interface would reasonably conclude that setting `default_followees` configures the behavior of new neurons.
- The TODO comments are internal implementation notes not surfaced to SNS operators or token holders.
- The validation that enforces emptiness provides no user-facing explanation that the feature is unimplemented; it simply rejects non-empty values with an error message that does not explain why.

---

### Recommendation

Either:

1. **Implement the feature**: Apply `default_followees` when a new neuron is claimed, consistent with the documented behavior.
2. **Remove the field from the public API**: If the feature is not intended to be supported, remove `default_followees` from `NervousSystemParameters` in the Candid interface and proto, and update documentation accordingly.
3. **At minimum, surface the limitation**: If the field must remain for schema compatibility, the validation error message and API documentation should explicitly state that the feature is not yet implemented, so SNS communities do not design governance models around it.

---

### Proof of Concept

An SNS operator reads the Candid interface:

```
type NervousSystemParameters = record {
  default_followees : opt DefaultFollowees;
  ...
};
```

They read the proto documentation: *"The set of default followees that every newly created neuron will follow per function."*

They submit a `ManageNervousSystemParameters` proposal with a non-empty `default_followees`. The proposal is **rejected** by `validate_default_followees` with the message:

> `DefaultFollowees.default_followees must be empty, but found {...}` [7](#0-6) 

Even if the field were accepted (e.g., at genesis with an empty map), the governance canister never reads `default_followees` when creating a new neuron — the `default_followees_or_panic` accessor is called but the returned empty `DefaultFollowees` struct has no effect on neuron initialization. New neurons are created with no followees, silently diverging from the documented behavior.

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

**File:** rs/sns/governance/src/governance.rs (L570-588)
```rust
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

**File:** rs/sns/governance/canister/governance.did (L557-570)
```text
type NervousSystemParameters = record {
  default_followees : opt DefaultFollowees;
  max_dissolve_delay_seconds : opt nat64;
  max_dissolve_delay_bonus_percentage : opt nat64;
  max_followees_per_function : opt nat64;
  neuron_claimer_permissions : opt NeuronPermissionList;
  neuron_minimum_stake_e8s : opt nat64;
  max_neuron_age_for_age_bonus : opt nat64;
  initial_voting_period_seconds : opt nat64;
  neuron_minimum_dissolve_delay_to_vote_seconds : opt nat64;
  reject_cost_e8s : opt nat64;
  max_proposals_to_keep_per_action : opt nat32;
  wait_for_quiet_deadline_increase_seconds : opt nat64;
  max_number_of_neurons : opt nat64;
```
