The asymmetry is **confirmed in the code**. This is a real finding.

---

### Title
Asymmetric API Boundary Node Version Coverage Between Pre-Guard and Post-Invariant Permanently Blocks Version Unelection — (`rs/registry/canister/src/mutations/do_revise_elected_replica_versions.rs`)

### Summary

`remove_blessed_versions_or_panic` (the pre-application guard) only checks subnets and unassigned nodes for in-use versions, but `check_replica_version_invariants` (the post-application invariant, called via `maybe_apply_mutation_internal`) also checks API boundary nodes. When a version is deployed to API boundary nodes and a governance proposal attempts to unelect it, the pre-guard passes, the mutations are prepared, and then the post-invariant panics — rolling back the mutation. The version can never be unelected while any API boundary node uses it.

### Finding Description

In `do_revise_elected_guestos_versions`, the guard `remove_blessed_versions_or_panic` iterates over subnets and unassigned nodes to verify the version is not in use: [1](#0-0) 

It does **not** iterate over API boundary node records. After this guard passes, the function calls `maybe_apply_mutation_internal`, which triggers `check_global_state_invariants` → `check_replica_version_invariants`.

`check_replica_version_invariants` explicitly collects API boundary node versions into `versions_in_use`: [2](#0-1) 

It then asserts the elected set is a superset of all versions in use, including API BN versions: [3](#0-2) 

Because the mutations include deleting the `ReplicaVersionRecord` for the version being unelected, the post-invariant check sees the version as "in use" (by API BNs) but "not elected" (record deleted), and panics with `"Using a version that isn't elected"`. The IC canister trap rolls back all state changes. The unelect proposal is consumed by governance but has no effect on the registry.

The helper `get_all_api_boundary_node_versions` confirms API BN versions are included in the invariant check: [4](#0-3) 

### Impact Explanation

Any version deployed to API boundary nodes via `do_deploy_guestos_to_some_api_boundary_nodes` becomes permanently unelectable through the normal `ReviseElectedGuestosVersions` governance flow. If that version later has a security vulnerability, the community cannot retire it — API boundary nodes are frozen on the vulnerable version until a separate proposal first migrates all API BNs to a different version. This is an operational security risk: the version lifecycle management for API boundary nodes is broken.

### Likelihood Explanation

This triggers on any routine version lifecycle operation: deploy version V to API BNs, then attempt to unelect V (e.g., after upgrading to V+1). No adversarial intent is required — the bug manifests in normal operations. Any NNS neuron holder can trigger it by submitting a `DeployGuestosToSomeApiBoundaryNodes` proposal followed by a `ReviseElectedGuestosVersions` unelect proposal.

### Recommendation

Add API boundary node version checking to `remove_blessed_versions_or_panic`, mirroring the logic already present in `check_replica_version_invariants`. Specifically, iterate over all `ApiBoundaryNodeRecord` entries and check whether any use a version in `versions_to_remove`, panicking with an appropriate message if so. This makes the pre-guard and post-invariant consistent in their coverage.

### Proof of Concept

State-machine test:
1. Initialize registry with version V elected and one API boundary node record with `version = V`.
2. Call `do_revise_elected_guestos_versions` with `replica_versions_to_unelect = [V]`.
3. Observe that `remove_blessed_versions_or_panic` does **not** panic (API BNs not checked).
4. Observe that `maybe_apply_mutation_internal` panics with `"Using a version that isn't elected. Elected versions: {}, in use: {"V"}"` — confirming the post-invariant fires on the API BN version.
5. Confirm the registry state is unchanged (V still elected, unelect proposal effectively a no-op).

The existing test `panic_when_retiring_a_version_in_use` at line 203 of `replica_version.rs` demonstrates the post-invariant fires for subnet versions; an analogous test for API BN versions would reproduce this bug. [5](#0-4) [6](#0-5)

### Citations

**File:** rs/registry/canister/src/mutations/do_revise_elected_replica_versions.rs (L55-55)
```rust
        let mut versions = self.remove_blessed_versions_or_panic(&versions_to_remove);
```

**File:** rs/registry/canister/src/mutations/do_revise_elected_replica_versions.rs (L104-107)
```rust
    pub fn remove_blessed_versions_or_panic(
        &self,
        versions_to_remove: &BTreeSet<String>,
    ) -> Vec<String> {
```

**File:** rs/registry/canister/src/mutations/do_revise_elected_replica_versions.rs (L121-167)
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

**File:** rs/registry/canister/src/invariants/replica_version.rs (L107-112)
```rust
fn get_all_api_boundary_node_versions(snapshot: &RegistrySnapshot) -> BTreeSet<String> {
    get_api_boundary_node_records_from_snapshot(snapshot)
        .values()
        .map(|node_record| node_record.version.clone())
        .collect()
}
```
