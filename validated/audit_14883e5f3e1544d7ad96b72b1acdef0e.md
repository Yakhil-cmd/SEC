Audit Report

## Title
Unbounded `followees` BTreeMap Key Growth Per Neuron via Repeated `ManageNeuron::Follow` Calls — (File: `rs/sns/governance/src/governance.rs`)

## Summary
The `follow()` function in SNS governance enforces a per-function limit on the number of followees but imposes no limit on the number of distinct `function_id` keys a neuron can accumulate in its `followees: BTreeMap<u64, Followees>`. Any neuron owner holding `Vote` permission can call `ManageNeuron::Follow` once per registered `function_id`, growing the neuron's serialized size proportionally to the total number of registered functions. If the aggregate state becomes large enough, `pre_upgrade` serialization can exceed the Wasm instruction limit, permanently blocking canister upgrades.

## Finding Description
In `rs/sns/governance/src/governance.rs`, the `follow()` function performs exactly two guards before inserting into `neuron.followees`:

1. **Lines 3991–3996**: rejects if `f.followees.len() > max_followees_per_function` — limits the number of followees *per function*, not the number of distinct function keys.
2. **Lines 3998–4006**: rejects if `f.function_id` is not in `id_to_nervous_system_functions` — ensures the function is registered, but does not bound how many registered functions a neuron may follow.

The unconditional insert at line 4032 executes for every valid, registered `function_id`:

```rust
neuron.followees.insert(
    f.function_id,
    Followees { followees: f.followees.clone() },
);
```

There is no check of the form `neuron.followees.len() < MAX_FOLLOWEE_FUNCTION_IDS` anywhere in `follow()`. An attacker who observes `N` registered `function_id`s can issue `N` sequential `Follow` calls, each adding one new key to `neuron.followees`, until the map contains `N` entries. The `function_followee_index` in the governance struct grows in parallel, compounding the state bloat across the entire canister.

## Impact Explanation
Each `BTreeMap` entry serializes to approximately 8 bytes (key) plus the encoded `Followees` message. With `MAX_NUMBER_OF_GENERIC_NERVOUS_SYSTEM_FUNCTIONS` defined in `rs/sns/governance/src/proposal.rs`, a single neuron's serialized size can grow to tens of megabytes if many generic functions are registered. During canister upgrade, `pre_upgrade` serializes the full governance state. If serialization time exceeds the Wasm instruction limit, the upgrade traps and the SNS governance canister becomes permanently unupgradeable — a concrete application/platform-level DoS matching the **High ($2,000–$10,000)** impact class: *"Application/platform-level DoS, crash, consensus blocking, certified-state disruption, or subnet availability impact not based on raw volumetric DDoS."*

## Likelihood Explanation
The precondition — an SNS with many registered generic functions — requires governance proposals to pass, which is a legitimate SNS operation. Once those functions exist (for any valid SNS purpose), **any** neuron owner with `Vote` permission (the standard, unprivileged permission) can exploit the missing limit. The attacker does not need to register the functions; they only need to observe which `function_id`s are registered and call `Follow` for each. The `Vote` permission is not privileged. The exploit is repeatable and requires no special resources beyond holding a neuron.

## Recommendation
Add a check in `follow()` before the insert at line 4032, bounding the total number of distinct `function_id` keys in `neuron.followees`:

```rust
if neuron.followees.len() >= MAX_FOLLOWEE_FUNCTION_IDS {
    return Err(GovernanceError::new_with_message(
        ErrorType::InvalidCommand,
        "Too many followed function IDs.",
    ));
}
```

`MAX_FOLLOWEE_FUNCTION_IDS` should be set independently of `MAX_NUMBER_OF_GENERIC_NERVOUS_SYSTEM_FUNCTIONS` and should reflect a safe per-neuron serialization budget. Additionally, consider enforcing a maximum neuron serialized size at the storage layer.

## Proof of Concept
1. Deploy an SNS and pass governance proposals to register `N` generic nervous system functions (legitimate SNS operation).
2. As a neuron owner with `Vote` permission, call `ManageNeuron::Follow` with `function_id = k` and `followees = [some_neuron_id]` for each `k` in `0..N`.
3. Assert `neuron.followees.len() == N` — no rejection occurs at any step.
4. Trigger a canister upgrade; observe that `pre_upgrade` serialization time grows linearly with `N`, eventually trapping when `N` is large enough to exceed the Wasm instruction limit.
5. Reproducible as a deterministic integration test using PocketIC: register `N` functions via proposals, execute `N` follow calls from a single neuron, then invoke the upgrade hook and assert it traps. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** rs/sns/governance/src/governance.rs (L3987-3996)
```rust
        // Check that the list of followees is not too
        // long. Allowing neurons to follow too many neurons
        // allows a memory exhaustion attack on the neurons
        // canister.
        if f.followees.len() > max_followees_per_function as usize {
            return Err(GovernanceError::new_with_message(
                ErrorType::InvalidCommand,
                "Too many followees.",
            ));
        }
```

**File:** rs/sns/governance/src/governance.rs (L3998-4006)
```rust
        if !is_registered_function_id(f.function_id, &self.proto.id_to_nervous_system_functions) {
            return Err(GovernanceError::new_with_message(
                ErrorType::NotFound,
                format!(
                    "Function with id: {} is not present among the current set of functions.",
                    f.function_id,
                ),
            ));
        }
```

**File:** rs/sns/governance/src/governance.rs (L4028-4037)
```rust
        if !f.followees.is_empty() {
            // Insert the new list of followees for this function_id in
            // the neuron's followees, removing the old list, which has
            // already been removed from the followee index above.
            neuron.followees.insert(
                f.function_id,
                Followees {
                    followees: f.followees.clone(),
                },
            );
```
