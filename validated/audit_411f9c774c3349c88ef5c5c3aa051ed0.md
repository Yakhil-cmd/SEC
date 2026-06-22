The code is directly visible. Let me read the exact lines for the two functions side by side.

The bug is confirmed. The two functions are directly comparable:

- `insert_upgrade_path_entry` (line 1956): declares `mut occupied` and calls `occupied.insert(to)` after logging.
- `insert_sns_specific_upgrade_path_entry` (line 1939): `occupied` is not `mut`, the `Occupied` branch only logs, and **`occupied.insert(to)` is never called**. The new value is silently dropped.

---

### Title
SNS-specific upgrade path entry cannot be overwritten — silent data loss in `insert_sns_specific_upgrade_path_entry` — (`rs/nns/sns-wasm/src/sns_wasm.rs`)

### Summary
`insert_sns_specific_upgrade_path_entry` is missing `occupied.insert(to)` in its `Entry::Occupied` branch. Any NNS governance proposal that attempts to update an already-existing SNS-specific upgrade path entry silently succeeds (returns `error: None`) but discards the new value, permanently locking the SNS to the original entry.

### Finding Description
In `UpgradePath::insert_sns_specific_upgrade_path_entry`:

```rust
// rs/nns/sns-wasm/src/sns_wasm.rs  lines 1939-1947
Entry::Occupied(occupied) => {          // `occupied` is NOT mut
    println!(
        "Special Entry for {} from {:?} to {:?} is being overwritten with new value {:?}",
        sns_governance_canister_id, occupied.key(), occupied.get(), to
    );
    // BUG: occupied.insert(to) is never called — `to` is dropped here
}
```

The sibling function `insert_upgrade_path_entry` (lines 1956–1963) correctly declares `mut occupied` and calls `occupied.insert(to)`. The SNS-specific variant is missing both the `mut` qualifier and the `insert` call. [1](#0-0) [2](#0-1) 

The caller `insert_upgrade_path_entries` propagates the silent success upward:

```rust
// lines 1627
InsertUpgradePathEntriesResponse { error: None }
``` [3](#0-2) 

### Impact Explanation
Once an SNS-specific upgrade path entry `A → B` is set for governance canister `G`, no subsequent NNS governance proposal can ever change it. Every follow-up call to `insert_sns_specific_upgrade_path_entry` with the same `from` key hits the `Occupied` branch, logs "being overwritten", and returns success — but the stored value remains `B`. The SNS is permanently locked to the original upgrade path entry. Because the response always carries `error: None`, neither the proposer nor any monitoring tooling can detect the failure.

### Likelihood Explanation
The `InsertSnsWasmUpgradePathEntries` NNS function is the intended mechanism for correcting a broken SNS upgrade path. Any scenario where NNS governance needs to revise a previously set SNS-specific entry — whether to fix a mistake, respond to a discovered bug, or update a stale path — will silently fail. This is a routine governance operation, not an exotic edge case.

### Recommendation
In `insert_sns_specific_upgrade_path_entry`, change the `Occupied` branch to mirror `insert_upgrade_path_entry`:

```rust
Entry::Occupied(mut occupied) => {   // add `mut`
    println!(...);
    occupied.insert(to);             // add this line
}
``` [1](#0-0) 

### Proof of Concept
State-machine test (unit level, no external dependencies):

1. Create a `SnsWasmCanister` with a deployed SNS for governance canister `G`.
2. Call `insert_upgrade_path_entries` with `current_version = A`, `next_version = B`, `sns_governance_canister_id = G`.
3. Assert `get_next_sns_version(A, G)` returns `B`. ✓
4. Call `insert_upgrade_path_entries` again with `current_version = A`, `next_version = C`, `sns_governance_canister_id = G`. Response: `error: None`.
5. Assert `get_next_sns_version(A, G)` returns `C`. **FAILS — returns `B`.**

The existing test `test_reconfigure_previous_upgrade_path_for_specific_sns` (line 3308) does not cover this case: it only inserts entries for `from` keys that were not previously set, so it never exercises the `Occupied` branch. [4](#0-3)

### Citations

**File:** rs/nns/sns-wasm/src/sns_wasm.rs (L610-627)
```rust
        if let Some(sns_governance_canister_id) = sns_governance_canister_id {
            for upgrade_step in upgrade_path {
                self.upgrade_path.insert_sns_specific_upgrade_path_entry(
                    upgrade_step.current_version.unwrap(),
                    upgrade_step.next_version.unwrap(),
                    sns_governance_canister_id,
                );
            }
        } else {
            for upgrade_step in upgrade_path {
                self.upgrade_path.insert_upgrade_path_entry(
                    upgrade_step.current_version.unwrap(),
                    upgrade_step.next_version.unwrap(),
                );
            }
        }

        InsertUpgradePathEntriesResponse { error: None }
```

**File:** rs/nns/sns-wasm/src/sns_wasm.rs (L1939-1951)
```rust
            Entry::Occupied(occupied) => {
                println!(
                    "Special Entry for {}  from {:?} to {:?} is being overwritten with new value {:?}",
                    sns_governance_canister_id,
                    occupied.key(),
                    occupied.get(),
                    to
                );
            }
            Entry::Vacant(vacant) => {
                vacant.insert(to);
            }
        };
```

**File:** rs/nns/sns-wasm/src/sns_wasm.rs (L1954-1969)
```rust
    pub fn insert_upgrade_path_entry(&mut self, from: SnsVersion, to: SnsVersion) {
        match self.upgrade_path.entry(from) {
            Entry::Occupied(mut occupied) => {
                println!(
                    "Entry from {:?} to {:?} is being overwritten with new value {:?}",
                    occupied.key(),
                    occupied.get(),
                    to
                );
                occupied.insert(to);
            }
            Entry::Vacant(vacant) => {
                vacant.insert(to);
            }
        };
        // self.upgrade_path.insert(from, to);
```

**File:** rs/nns/sns-wasm/src/sns_wasm.rs (L3307-3406)
```rust
    #[test]
    fn test_reconfigure_previous_upgrade_path_for_specific_sns() {
        // In this test, we use a combination of (1) add_wasm without updating latest version and
        // (2) insert_upgrade_path_entries to reconfigure a previous upgrade path for a specific SNS
        let mut canister = new_wasm_canister();
        let normal_governance_canister_id = CanisterId::from_u64(1);
        let special_governance_canister_id = CanisterId::from_u64(1000);
        // Prepare the deployed SNS list for the test, since inserting custom upgrade path entries
        // requires a deployed SNS.
        canister.deployed_sns_list.push(DeployedSns {
            root_canister_id: Some(CanisterId::from_u64(999).into()),
            governance_canister_id: Some(special_governance_canister_id.get()),
            ledger_canister_id: Some(CanisterId::from_u64(1001).into()),
            swap_canister_id: Some(CanisterId::from_u64(1002).into()),
            index_canister_id: Some(CanisterId::from_u64(1003).into()),
        });

        let mut add_wasm_and_return_hash =
            |sns_type: SnsCanisterType, id: u32, skip_update_latest_version: bool| {
                let wasm = SnsWasm {
                    canister_type: sns_type as i32,
                    ..small_valid_wasm_with_id(format!("{} {}", sns_type.as_str_name(), id))
                };
                let hash = wasm.sha256_hash();
                let response = canister.add_wasm(AddWasmRequest {
                    wasm: Some(wasm),
                    hash: hash.to_vec(),
                    skip_update_latest_version: Some(skip_update_latest_version),
                });

                assert_eq!(
                    response,
                    AddWasmResponse {
                        result: Some(add_wasm_response::Result::Hash(hash.to_vec())),
                    }
                );

                hash.to_vec()
            };

        // Below is the "normal" upgrade path
        let governance_1_hash = add_wasm_and_return_hash(SnsCanisterType::Governance, 1, false);
        let root_1_hash = add_wasm_and_return_hash(SnsCanisterType::Root, 1, false);
        let ledger_1_hash = add_wasm_and_return_hash(SnsCanisterType::Ledger, 1, false);
        let swap_1_hash = add_wasm_and_return_hash(SnsCanisterType::Swap, 1, false);
        let archive_1_hash = add_wasm_and_return_hash(SnsCanisterType::Archive, 1, false);
        let index_1_hash = add_wasm_and_return_hash(SnsCanisterType::Index, 1, false);
        let governance_2_hash = add_wasm_and_return_hash(SnsCanisterType::Governance, 2, false);

        let basic_version = SnsVersion {
            governance_wasm_hash: governance_1_hash,
            root_wasm_hash: root_1_hash,
            ledger_wasm_hash: ledger_1_hash,
            swap_wasm_hash: swap_1_hash,
            archive_wasm_hash: archive_1_hash,
            index_wasm_hash: index_1_hash,
        };
        // Add a "special" root wasm that is not in the normal upgrade path.
        let root_2_hash = add_wasm_and_return_hash(SnsCanisterType::Root, 2, true);

        // Assert that the upgrade path for the normal governance canister does not contain the
        // special wasm, even before the insert_upgrade_path_entries call.
        assert_eq!(
            canister.get_next_sns_version(
                GetNextSnsVersionRequest {
                    current_version: Some(SnsVersion {
                        governance_wasm_hash: governance_2_hash.clone(),
                        ..basic_version.clone()
                    }),
                    governance_canister_id: Some(normal_governance_canister_id.get()),
                },
                PrincipalId::new_user_test_id(1),
            ),
            GetNextSnsVersionResponse { next_version: None }
        );

        let response = canister.insert_upgrade_path_entries(InsertUpgradePathEntriesRequest {
            upgrade_path: vec![
                SnsUpgrade {
                    current_version: Some(basic_version.clone()),
                    next_version: Some(SnsVersion {
                        root_wasm_hash: root_2_hash.clone(),
                        ..basic_version.clone()
                    }),
                },
                SnsUpgrade {
                    current_version: Some(SnsVersion {
                        root_wasm_hash: root_2_hash.clone(),
                        ..basic_version.clone()
                    }),
                    next_version: Some(SnsVersion {
                        root_wasm_hash: root_2_hash.clone(),
                        governance_wasm_hash: governance_2_hash.clone(),
                        ..basic_version.clone()
                    }),
                },
            ],
            sns_governance_canister_id: Some(special_governance_canister_id.get()),
        });
        assert_eq!(response, InsertUpgradePathEntriesResponse { error: None });
```
