### Title
Unbounded Full-Store Scan in `get_node_operators_and_dcs_of_node_provider` Causes Permanent Query DoS as Registry Grows - (File: rs/registry/canister/src/get_node_operators_and_dcs_of_node_provider.rs)

### Summary
The registry canister's `get_node_operators_and_dcs_of_node_provider` function performs a full linear scan over the entire `self.store` BTreeMap — which contains every registry entry (nodes, subnets, node operators, data centers, replica versions, etc.) — to find node operators belonging to a given node provider. As the IC network grows organically, the store grows without bound. Eventually this query will permanently exceed the IC instruction limit and become impossible to call, with no pagination or batching mechanism available.

### Finding Description
In `rs/registry/canister/src/get_node_operators_and_dcs_of_node_provider.rs`, the function iterates over every key-value pair in `self.store`:

```rust
for (key, values) in self.store.iter() {
    if key.starts_with(NODE_OPERATOR_RECORD_KEY_PREFIX.as_bytes()) {
        // decode NodeOperatorRecord, decode DataCenterRecord, push result
    }
}
``` [1](#0-0) 

The `self.store` is a `BTreeMap` that accumulates every registry key ever written — nodes, subnets, node operators, data centers, replica versions, firewall rules, routing table shards, etc. The loop does not filter by prefix before iterating; it checks `key.starts_with(NODE_OPERATOR_RECORD_KEY_PREFIX.as_bytes())` inside the loop body, meaning every single registry entry is visited. For each matching entry, it also decodes a protobuf `NodeOperatorRecord` and performs a secondary lookup for the `DataCenterRecord`, both of which consume additional instructions.

There is no pagination, no early-exit limit, and no way to resume a partial scan. The function is exposed as a canister endpoint callable by any unprivileged principal. [2](#0-1) 

### Impact Explanation
Once the registry store grows large enough that a single call to this function exceeds the IC instruction limit (`CanisterInstructionLimitExceeded`), the function becomes permanently and irrecoverably unusable — there is no way to call it in smaller batches because the API accepts only a single `node_provider` principal and returns all results at once. Any downstream consumer of this endpoint (e.g., tooling that queries node operator/data center associations for a given node provider) loses access to this data permanently. If this function is on the critical path for node provider reward auditing or display, that functionality is also permanently broken. [3](#0-2) 

### Likelihood Explanation
The IC network grows continuously: new nodes, subnets, node operators, data centers, replica versions, and routing table entries are added regularly via NNS governance proposals. Each addition increases `self.store`. The registry also retains historical versions of every key (the `VecDeque` per key), so the store grows monotonically and never shrinks. No attacker action is required — organic network growth is sufficient. The IC already has hundreds of nodes and dozens of subnets; at scale (thousands of nodes), the per-call instruction cost of iterating and decoding every entry will cross the limit.

### Recommendation
Replace the full-store scan with a prefix-range scan using `self.store.range(start..)` with a `take_while` on the node-operator prefix — exactly the pattern already used in `get_key_family_raw_iter_at_version`: [4](#0-3) 

Alternatively, expose a paginated version of the endpoint that accepts a `start_after` key and a `limit`, allowing callers to retrieve results in bounded chunks.

### Proof of Concept
1. Observe that `self.store` in the registry canister is a `BTreeMap<Vec<u8>, VecDeque<HighCapacityRegistryValue>>` containing all registry entries.
2. Call `get_node_operators_and_dcs_of_node_provider(some_principal)` on a registry canister whose store has grown to contain tens of thousands of entries (nodes, subnets, routing table shards, firewall rules, etc.).
3. The loop at line 23 visits every entry, decoding protobuf for each node-operator-prefixed key and performing a secondary `self.get(dc_key)` lookup.
4. When total instructions consumed exceed the IC per-message instruction limit (40 billion instructions on application subnets), the call traps with `CanisterInstructionLimitExceeded`.
5. Because the function has no pagination parameter, no subsequent call can succeed — the function is permanently DoS'd. [1](#0-0)

### Citations

**File:** rs/registry/canister/src/get_node_operators_and_dcs_of_node_provider.rs (L15-18)
```rust
    pub fn get_node_operators_and_dcs_of_node_provider(
        &self,
        node_provider: PrincipalId,
    ) -> Result<Vec<(DataCenterRecord, NodeOperatorRecord)>, String> {
```

**File:** rs/registry/canister/src/get_node_operators_and_dcs_of_node_provider.rs (L23-66)
```rust
        for (key, values) in self.store.iter() {
            if key.starts_with(NODE_OPERATOR_RECORD_KEY_PREFIX.as_bytes()) {
                let value: &HighCapacityRegistryValue = values.back().as_ref().unwrap();

                let node_operator = with_chunks(|chunks| {
                    decode_high_capacity_registry_value::<NodeOperatorRecord, _>(value, chunks)
                });

                let Some(node_operator) = node_operator else {
                    continue;
                };

                let node_provider_id = PrincipalId::try_from(
                    &node_operator.node_provider_principal_id,
                )
                .map_err(|e| {
                    format!(
                        "Node Operator with key '{:?}' has a node_provider_principal_id \
                                 that cannot be parsed as a PrincipalId: '{}'",
                        from_utf8(key.as_slice()),
                        e
                    )
                })?;
                if node_provider_id != node_provider {
                    continue;
                }
                let dc_id = node_operator.dc_id.clone();
                let dc_key = make_data_center_record_key(&dc_id);
                let dc_record_bytes = &self
                    .get(dc_key.as_bytes(), self.latest_version())
                    .ok_or_else(|| {
                        format!(
                            "Node Operator with key '{:?}' has data center ID '{}' \
                            not found in the Registry",
                            from_utf8(key.as_slice()),
                            dc_id
                        )
                    })?
                    .value;
                let data_center = DataCenterRecord::decode(dc_record_bytes.as_slice()).unwrap();
                node_operators_and_dcs_of_node_provider
                    .push((data_center.clone(), node_operator.clone()));
            }
        }
```

**File:** rs/registry/canister/canister/canister.rs (L1-1)
```rust
use candid::{Decode, candid_method};
```

**File:** rs/registry/canister/src/common/key_family.rs (L48-77)
```rust
pub(crate) fn get_key_family_raw_iter_at_version<'a>(
    registry: &'a Registry,
    prefix: &'a str,
    version: u64,
) -> impl Iterator<Item = (String, &'a HighCapacityRegistryValue)> + 'a {
    let prefix_bytes = prefix.as_bytes();
    let start = prefix_bytes.to_vec();

    // Note, using the 'store' which is a BTreeMap is what guarantees the order of keys.
    registry
        .store
        .range(start..)
        .take_while(|(k, _)| k.starts_with(prefix_bytes))
        .filter_map(move |(key, values)| {
            let latest_value: &HighCapacityRegistryValue =
                values.iter().rev().find(|value| value.version <= version)?;

            if !latest_value.is_present() {
                return None; // Deleted or otherwise empty value.
            }

            let id = key
                .strip_prefix(prefix_bytes)
                .and_then(|v| std::str::from_utf8(v).ok())
                .unwrap()
                .to_string();

            Some((id, latest_value))
        })
}
```
