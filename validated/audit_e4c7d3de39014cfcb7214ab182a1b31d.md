### Title
SNS Governance `NervousSystemParameters.default_followees` Not Applied to Newly Created Neurons - (File: `rs/sns/governance/src/governance.rs`)

### Summary
The `default_followees` field in SNS `NervousSystemParameters` is explicitly documented as having "no effect" across multiple authoritative files, yet it is accepted and stored when updated via a `ManageNervousSystemParameters` governance proposal. This is a direct analog to the reported vulnerability: a parameter that can be updated via a governance action is not applied to newly created instances (neurons), leaving them with a stale empty default regardless of what the community configured.

### Finding Description
`NervousSystemParameters.default_followees` is intended to specify the set of followees that every newly created neuron will follow per proposal function. The field is accepted in `ManageNervousSystemParameters` proposals and stored in governance state. However, the field is explicitly marked as having no effect in multiple authoritative locations:

- Proto definition explicitly states the feature is unimplemented: [1](#0-0) 

- The generated API type carries the same warning: [2](#0-1) 

- The governance implementation itself marks the helper as unsupported: [3](#0-2) 

When `claim_neuron` creates a new neuron, it calls `self.default_followees_or_panic().followees` at line 4342, but the default value initialized in `default_nervous_system_parameters()` is always an empty `BTreeMap`: [4](#0-3) 

The neuron creation path that uses this value:
<cite repo="hirayap/

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

**File:** rs/sns/governance/api/src/ic_sns_governance.pb.v1.rs (L1196-1205)
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
    pub default_followees: Option<DefaultFollowees>,
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

**File:** rs/sns/governance/api_helpers/src/lib.rs (L29-31)
```rust
        default_followees: Some(DefaultFollowees {
            followees: btreemap! {},
        }),
```
