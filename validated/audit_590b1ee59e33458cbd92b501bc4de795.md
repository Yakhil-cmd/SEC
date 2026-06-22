### Title
`do_delete_subnet()` Does Not Remove Subnet from `ChainKeyEnabledSubnetList` Registry Entries — (`File: rs/registry/canister/src/mutations/do_delete_subnet.rs`)

---

### Summary

`do_delete_subnet()` in the Registry canister performs incomplete cleanup when deleting a CloudEngine subnet. It removes the subnet record, CUP contents, threshold signing pubkey, routing table shards, and the subnet list entry — but it never removes the deleted subnet's ID from the `ChainKeyEnabledSubnetList` registry entries (keyed by `make_chain_key_enabled_subnet_list_key`). This leaves stale registry state that advertises the deleted subnet as a valid chain-key signer, which can cause chain-key signing requests to be routed to a non-existent subnet.

---

### Finding Description

`do_delete_subnet()` applies five categories of mutations: [1](#0-0) 

It removes the subnet from `subnet_list`, deletes the CUP contents key, the threshold signing pubkey, the subnet record, and routing table shards. However, it never touches the `ChainKeyEnabledSubnetList` entries.

When a subnet is configured with chain key signing (e.g., ECDSA, Schnorr, VetKD), its subnet ID is written into the registry under `make_chain_key_enabled_subnet_list_key(&key_id)`: [2](#0-1) 

The Registry canister already has a helper, `mutations_to_disable_subnet_chain_key()`, that correctly removes a subnet from these lists: [3](#0-2) 

This helper is called in `do_update_subnet` when chain key signing is disabled, but it is **never called** in `do_delete_subnet`. The PocketIC `delete_subnet` implementation demonstrates the correct pattern — it explicitly calls `remove_chain_key_registry_records` before finalizing the deletion: [4](#0-3) 

---

### Impact Explanation

After a CloudEngine subnet with chain key signing enabled is deleted, the registry continues to list the deleted subnet's ID inside `ChainKeyEnabledSubnetList` for every key it was enabled for. Consumers of this list — including the NNS governance canister and chain-fusion components (ckBTC, ckETH, etc.) that route threshold signing requests — will observe the deleted subnet as a valid signer. Signing requests for those keys may be routed to the non-existent subnet, causing them to fail. This breaks chain-key signing availability for any key that was exclusively or partially served by the deleted subnet, and leaves the registry in a permanently inconsistent state that cannot be corrected without a separate governance proposal.

---

### Likelihood Explanation

CloudEngine subnets are explicitly designed to be deletable (the only subnet type for which `delete_subnet` is permitted). The `delete_engine` entry point in the engine controller canister is a normal operational path: [5](#0-4) 

Any NNS neuron holder can submit a governance proposal to delete a CloudEngine subnet. If that subnet has chain key signing enabled — a realistic configuration for a compute subnet — the incomplete cleanup is triggered automatically upon proposal execution. No special attacker capability is required beyond submitting a governance proposal.

---

### Recommendation

In `do_delete_subnet()`, after reading the subnet record (which already happens to check the subnet type), extract the list of chain key IDs from `subnet_record.chain_key_config`, call `self.mutations_to_disable_subnet_chain_key(subnet_id_, &key_ids)`, and append the resulting mutations to the batch before calling `maybe_apply_mutation_internal`. This mirrors the cleanup already performed by `do_update_subnet` when chain key signing is disabled, and matches the behavior of the PocketIC `delete_subnet` implementation.

---

### Proof of Concept

1. Create a CloudEngine subnet with chain key signing enabled for key `ecdsa_key_1`. The registry now contains a `ChainKeyEnabledSubnetList` entry at `master_public_key_id_ecdsa_key_1` listing the subnet.
2. Submit and execute a governance proposal calling `delete_subnet` with the CloudEngine subnet's ID.
3. `do_delete_subnet` succeeds and removes the subnet record, CUP, pubkey, and routing table entries.
4. Query the registry for `make_chain_key_enabled_subnet_list_key(&ecdsa_key_1)`. The deleted subnet's ID is still present in the list.
5. Any subsequent chain-key signing request for `ecdsa_key_1` that consults this list will attempt to route to the deleted subnet, resulting in a failure.

The existing test `cloud_engine_subnet_can_be_deleted_by` in `rs/registry/canister/tests/delete_subnet.rs` only asserts that the subnet is removed from `subnet_list` and does not verify that `ChainKeyEnabledSubnetList` entries are cleaned up, confirming the gap is untested: [6](#0-5)

### Citations

**File:** rs/registry/canister/src/mutations/do_delete_subnet.rs (L44-84)
```rust
        // Remove from `subnet_list`.
        let mut subnet_list = self.get_subnet_list_record().subnets;
        let len_before = subnet_list.len();
        subnet_list.retain(|s| s != subnet_id.as_slice());
        if subnet_list.len() > len_before - 1 {
            println!(
                "{LOG_PREFIX}do_delete_subnet: Subnet {} was not present in subnet_list.",
                subnet_id
            );
        }
        let new_subnet_list_record = SubnetListRecord {
            subnets: subnet_list,
        };
        let subnet_list_mutation = update(
            make_subnet_list_record_key().as_bytes(),
            new_subnet_list_record.encode_to_vec(),
        );

        // Remove catch up package.
        let subnet_dkg_mutation = delete(make_catch_up_package_contents_key(subnet_id_).as_bytes());

        // Remove pubkey.
        let subnet_threshold_signing_pubkey_mutation =
            delete(make_crypto_threshold_signing_pubkey_key(subnet_id_).as_bytes());

        // Remove subnet record.
        let remove_subnet_mutation = delete(make_subnet_record_key(subnet_id_).into_bytes());

        // Remove routing table shards.
        let mut remove_from_routing_table_mutations =
            self.remove_subnet_from_routing_table(self.latest_version(), subnet_id_);
        let mut mutations = vec![
            subnet_list_mutation,
            subnet_dkg_mutation,
            subnet_threshold_signing_pubkey_mutation,
            remove_subnet_mutation,
        ];
        mutations.append(&mut remove_from_routing_table_mutations);

        // Check invariants before applying mutations
        self.maybe_apply_mutation_internal(mutations);
```

**File:** rs/prep/src/initialized_subnet.rs (L93-111)
```rust
            if let Some(chain_key_config) = &self.subnet_config.chain_key_config {
                for key_id in chain_key_config
                    .key_configs
                    .iter()
                    .map(|config| config.key_id.clone().unwrap())
                {
                    let key_id = MasterPublicKeyId::try_from(key_id)
                        .unwrap_or_else(|err| panic!("Invalid key_id {err}"));
                    write_registry_entry(
                        data_provider,
                        subnet_path.as_path(),
                        make_chain_key_enabled_subnet_list_key(&key_id).as_ref(),
                        version,
                        ChainKeyEnabledSubnetList {
                            subnets: vec![subnet_id_into_protobuf(subnet_id)],
                        },
                    );
                }
            }
```

**File:** rs/registry/canister/src/mutations/subnet.rs (L293-325)
```rust
    /// Create the mutations that disable set of chain keys for a single subnet.
    pub fn mutations_to_disable_subnet_chain_key(
        &self,
        subnet_id: SubnetId,
        chain_key_disable: &Vec<MasterPublicKeyId>,
    ) -> Vec<RegistryMutation> {
        let mut mutations = vec![];
        for chain_key_id in chain_key_disable {
            let mut chain_key_signing_list_for_key = self
                .get_chain_key_enabled_subnet_list(chain_key_id)
                .unwrap_or_default();

            // If that key is already disabled on this subnet, do nothing.
            if !chain_key_signing_list_for_key
                .subnets
                .contains(&subnet_id_into_protobuf(subnet_id))
            {
                continue;
            }

            let protobuf_subnet_id = subnet_id_into_protobuf(subnet_id);
            // Preconditions are okay, so we remove the subnet from our list of signing subnets.
            chain_key_signing_list_for_key
                .subnets
                .retain(|subnet| subnet != &protobuf_subnet_id);

            mutations.push(upsert(
                make_chain_key_enabled_subnet_list_key(chain_key_id),
                chain_key_signing_list_for_key.encode_to_vec(),
            ));
        }
        mutations
    }
```

**File:** rs/pocket_ic_server/src/pocket_ic.rs (L2832-2860)
```rust
        // Update global registry records to reflect the removed subnet.
        if self.nns_subnet.is_some() {
            let next_version =
                RegistryVersion::new(self.registry_data_provider.latest_version().get() + 1);
            remove_chain_key_registry_records(
                &empty_chain_key_ids,
                self.registry_data_provider.clone(),
                next_version,
            );
            let subnet_list = self
                .subnets
                .get_all()
                .into_iter()
                .map(|s| s.get_subnet_id())
                .collect();
            update_global_registry_records(
                next_version,
                self.routing_table.clone(),
                subnet_list,
                self.chain_keys.clone(),
                self.registry_data_provider.clone(),
            );
            remove_subnet_local_registry_records(
                subnet_id,
                &subnet.state_machine.nodes,
                self.registry_data_provider.clone(),
                next_version,
            );
            self.persist_registry_changes();
```

**File:** rs/engine_controller/canister/canister.rs (L171-188)
```rust
#[update]
async fn delete_engine(args: DeleteEngineArgs) -> Result<(), String> {
    ensure_authorized()?;

    let payload = DeleteSubnetPayload {
        subnet_id: args.subnet_id,
    };

    let response: Result<(), String> =
        Call::unbounded_wait(REGISTRY_CANISTER_ID.into(), "delete_subnet")
            .with_arg(payload)
            .await
            .map_err(|e| format!("registry.delete_subnet call failed: {e:?}"))?
            .candid()
            .map_err(|e| format!("Failed to decode registry response: {e}"))?;

    response
}
```

**File:** rs/registry/canister/tests/delete_subnet.rs (L256-266)
```rust

    // The subnet should no longer be in the subnet list.
    let subnets =
        decode_registry_value::<SubnetListRecordPb>(&pocket_ic, make_subnet_list_record_key())
            .await
            .subnets;
    assert!(
        !subnets.contains(&cloud_engine_subnet_id.get().to_vec()),
        "the cloud engine subnet should have been removed from the subnet list"
    );
}
```
