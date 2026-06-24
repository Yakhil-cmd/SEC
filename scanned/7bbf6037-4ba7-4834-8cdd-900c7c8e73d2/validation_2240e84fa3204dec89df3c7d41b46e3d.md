Let me verify the exact gap in `remove_blessed_versions_or_panic` and confirm the invariant check flow.

### Title
Missing API Boundary Node Version Check in `remove_blessed_versions_or_panic` Allows Permanent Registry DoS — (`rs/registry/canister/src/mutations/do_revise_elected_replica_versions.rs`)

---

### Summary

`remove_blessed_versions_or_panic` guards against unelecting a GuestOS version that is still in use, but its in-use check covers only **subnets** and **unassigned nodes** — it has no corresponding check for **API boundary nodes**. The global invariant enforcer `check_replica_version_invariants`, however, *does* include API BN versions in its superset assertion. Once the two governance proposals described below are executed in sequence, every subsequent call to `maybe_apply_mutation_internal` panics unconditionally, permanently halting all NNS-driven registry mutations.

---

### Finding Description

**Step 1 — Add API BNs with version V.**

`do_add_api_boundary_nodes` calls `check_replica_version_is_elected` before inserting the records, so version V must be elected at this point. The records are written with `version: V`. [1](#0-0) 

**Step 2 — Unelect version V.**

`do_revise_elected_guestos_versions` calls `remove_blessed_versions_or_panic`. That function checks subnets: [2](#0-1) 

…and unassigned nodes: [3](#0-2) 

There is **no analogous block for API boundary nodes**. The function returns successfully, and `maybe_apply_mutation_internal` commits the deletion of V's `ReplicaVersionRecord`.

**Step 3 — Any subsequent registry mutation panics.**

Every mutation path goes through `maybe_apply_mutation_internal` → `verify_mutations_internal` → `check_global_state_invariants` → `check_replica_version_invariants`. [4](#0-3) 

`check_replica_version_invariants` builds `versions_in_use` by appending API BN versions: [5](#0-4) 

It then asserts the elected set is a superset: [6](#0-5) 

Because V is still referenced by the API BN records but is no longer in the elected set, this `assert!` panics with `"Using a version that isn't elected."` on **every** future mutation, regardless of what that mutation touches.

---

### Impact Explanation

The registry canister is the single source of truth for all IC topology. Once the invariant is violated, no governance proposal that touches the registry can be executed — subnet upgrades, node additions, key rotations, firewall changes, etc. all call `maybe_apply_mutation_internal` and will trap. The only recovery path is a canister reinstall or upgrade that bypasses the invariant check, which itself requires a governance proposal — creating a circular dependency. The effective impact is a permanent halt of NNS governance operations over the registry.

---

### Likelihood Explanation

The two proposals are individually routine and non-suspicious:

1. "Add these nodes as API boundary nodes running version V" — standard operational proposal.
2. "Retire version V" — standard housekeeping proposal after a version is superseded.

Neither proposal, viewed in isolation, signals malicious intent. The missing guard means the system does not prevent this combination even when both proposals pass through normal NNS voting. A governance participant who understands the bug can craft and submit both proposals; they need only the usual voting majority, not any privileged key or out-of-band access.

---

### Recommendation

Add an API boundary node in-use check inside `remove_blessed_versions_or_panic`, mirroring the existing subnet and unassigned-node checks:

```rust
// After the unassigned-nodes check, before returning after_removal:
let api_bn_versions_in_use: BTreeSet<String> = get_api_boundary_node_records_from_snapshot(...)
    .values()
    .map(|r| r.version.clone())
    .filter(|v| versions_to_remove.contains(v))
    .collect();

if !api_bn_versions_in_use.is_empty() {
    panic!(
        "Cannot retire versions {api_bn_versions_in_use:?}, \
         because they are currently in use by API boundary nodes!"
    );
}
```

This makes the pre-mutation guard consistent with the post-mutation invariant.

---

### Proof of Concept

```rust
// State-machine test sketch
let mut registry = invariant_compliant_registry(0);

// 1. Elect version "V"
registry.maybe_apply_mutation_internal(vec![insert(
    make_replica_version_key("V").as_bytes(),
    ReplicaVersionRecord { release_package_sha256_hex: MOCK_HASH.into(),
                           release_package_urls: vec![MOCK_URL.into()],
                           guest_launch_measurements: None }.encode_to_vec(),
)]);

// 2. Add an API BN with version "V"
registry.do_add_api_boundary_nodes(AddApiBoundaryNodesPayload {
    node_ids: vec![some_node_id],
    version: "V".into(),
});

// 3. Unelect "V" — succeeds because remove_blessed_versions_or_panic
//    does not check API BNs
registry.do_revise_elected_guestos_versions(ReviseElectedGuestosVersionsPayload {
    replica_version_to_elect: None,
    replica_versions_to_unelect: vec!["V".into()],
    ..Default::default()
});

// 4. Any subsequent mutation panics:
//    "Using a version that isn't elected. ... in use: {"V"}"
registry.maybe_apply_mutation_internal(vec![/* any harmless mutation */]);
// ^ PANICS — registry is permanently bricked
```

### Citations

**File:** rs/registry/canister/src/mutations/do_add_api_boundary_nodes.rs (L96-98)
```rust
        // Ensure version exists and is elected
        check_replica_version_is_elected(self, &payload.version);
    }
```

**File:** rs/registry/canister/src/mutations/do_revise_elected_replica_versions.rs (L121-150)
```rust
        // Get all subnet records
        let subnets_key = make_subnet_list_record_key();
        let subnets = self
            .get(subnets_key.as_bytes(), version)
            .map(|reg_value| {
                SubnetListRecord::decode(reg_value.value.as_slice())
                    .unwrap()
                    .subnets
            })
            .unwrap_or_default();

        // Try to find a replica version that is both, part of the payload and used by a subnet
        let in_use = subnets
            .iter()
            .map(|id| {
                let subnet_id = SubnetId::new(PrincipalId::try_from(id).unwrap());
                let subnet_key = make_subnet_record_key(subnet_id);
                let reg_value = self.get(subnet_key.as_bytes(), version).unwrap();
                SubnetRecord::decode(reg_value.value.as_slice())
                    .unwrap()
                    .replica_version_id
            })
            .filter(|id| versions_to_remove.contains(id))
            .collect::<BTreeSet<String>>();

        if !in_use.is_empty() {
            panic!(
                "{LOG_PREFIX}Cannot retire versions {in_use:?}, because they are currently deployed to a subnet!"
            );
        }
```

**File:** rs/registry/canister/src/mutations/do_revise_elected_replica_versions.rs (L152-167)
```rust
        // Do the same for unassigned node record
        let unassigned_key = make_unassigned_nodes_config_record_key();
        let in_use = self
            .get(unassigned_key.as_bytes(), version)
            .map(|reg_value| {
                UnassignedNodesConfigRecord::decode(reg_value.value.as_slice())
                    .unwrap()
                    .replica_version
            })
            .filter(|id| versions_to_remove.contains(id));

        if let Some(version) = in_use {
            panic!(
                "{LOG_PREFIX}Cannot retire version {version}, because it is currently deployed to unassigned nodes!"
            );
        }
```

**File:** rs/registry/canister/src/registry.rs (L360-391)
```rust
    pub fn maybe_apply_mutation_internal(&mut self, mutations: Vec<RegistryMutation>) {
        println!(
            "{}Received a mutate call containing a list of {} mutations",
            LOG_PREFIX,
            mutations.len()
        );
        self.verify_mutations_internal(&mutations);
        self.apply_mutations(mutations);
    }

    #[cfg(any(test, feature = "canbench-rs"))]
    pub fn apply_mutations_for_test(&mut self, mutations: Vec<RegistryMutation>) {
        self.apply_mutations(mutations);
    }

    /// Checks that invariants would hold after applying the mutations
    pub(crate) fn verify_mutations_internal(&self, mutations: &Vec<RegistryMutation>) {
        let errors = self.verify_mutation_type(mutations.as_slice());
        if !errors.is_empty() {
            panic!(
                "{}Verification of the mutation type failed with the following errors: [{}].",
                LOG_PREFIX,
                errors
                    .iter()
                    .map(|e| format!("{e}"))
                    .collect::<Vec::<String>>()
                    .join(", ")
            );
        }

        self.check_global_state_invariants(mutations.as_slice());
    }
```

**File:** rs/registry/canister/src/invariants/replica_version.rs (L43-43)
```rust
    versions_in_use.append(&mut get_all_api_boundary_node_versions(snapshot));
```

**File:** rs/registry/canister/src/invariants/replica_version.rs (L55-58)
```rust
    assert!(
        elected_set.is_superset(&versions_in_use),
        "Using a version that isn't elected. Elected versions: {elected_set:?}, in use: {versions_in_use:?}."
    );
```
