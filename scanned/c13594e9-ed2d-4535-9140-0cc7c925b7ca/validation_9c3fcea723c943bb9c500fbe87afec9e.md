### Title
SNS-WASM Canister Has No Mechanism to Remove a Registered WASM - (`rs/nns/sns-wasm/src/sns_wasm.rs`)

### Summary
The SNS-WASM canister exposes `add_wasm` to register new SNS canister WASMs but provides no corresponding `remove_wasm` or `delete_wasm` function. Once a WASM is inserted into `wasm_indexes` and written to stable memory, it cannot be removed even via NNS governance proposal. If a WASM is later found to be vulnerable, the NNS cannot expunge it from the registry.

### Finding Description
In `rs/nns/sns-wasm/src/sns_wasm.rs`, the `add_wasm` function inserts the WASM hash into `self.wasm_indexes` (a `BTreeMap`) and writes the binary to stable memory:

```rust
self.wasm_indexes.insert(
    hash,
    SnsWasmStableIndex { hash: hash.to_vec(), offset, size, metadata },
);
```

A grep across the entire `rs/nns/sns-wasm/` tree for `remove_wasm`, `delete_wasm`, or `wasm_indexes.remove` returns zero matches. The canister's Candid interface (`rs/nns/sns-wasm/canister/sns-wasm.did`) exposes only `add_wasm` with no counterpart removal method.

The `SnsWasmCanister` struct holds `wasm_indexes: BTreeMap<[u8; 32], SnsWasmStableIndex>` and `deployed_sns_list: Vec<DeployedSns>`. Both grow monotonically; neither has a removal path.

The upgrade path can be redirected via `insert_upgrade_path_entries` to avoid routing SNS instances to a vulnerable version, but:
1. The WASM binary remains in stable memory and is still retrievable via `get_wasm`.
2. Any SNS instance that has already upgraded to the vulnerable version cannot be "un-upgraded" by removing the WASM.
3. The `insert_upgrade_path_entries` function itself validates that all WASM hashes in the submitted path exist in `wasm_indexes`, meaning the vulnerable WASM hash will always pass that check and could be re-introduced into a path by a future proposal. [1](#0-0) [2](#0-1) [3](#0-2) 

### Impact Explanation
If a vulnerable SNS canister WASM (governance, ledger, root, swap, or index) is added to SNS-WASM and SNS instances upgrade to it, the NNS has no direct mechanism to remove the vulnerable WASM from the registry. The upgrade path can be redirected, but the WASM remains permanently accessible. SNS instances already running the vulnerable code cannot be forced to downgrade via this mechanism. This is a direct analog to the BondAggregator issue: a registered entity (WASM) cannot be removed from the protocol once added, limiting the NNS's ability to respond swiftly to a discovered vulnerability. [4](#0-3) [5](#0-4) 

### Likelihood Explanation
Moderate. NNS governance proposals to add SNS WASMs (`NnsFunction::AddSnsWasm`) are routine release operations. A WASM could be added that contains a latent bug discovered only after deployment. The NNS would have no direct way to remove it. The initial addition requires NNS governance majority (high bar), but the scenario of a good-faith addition of a WASM with an undiscovered vulnerability is realistic and has precedent in other protocol ecosystems. [6](#0-5) 

### Recommendation
Add a `remove_wasm` update method callable only by NNS Governance (mirroring the `add_wasm` access control pattern) that:
1. Removes the hash entry from `wasm_indexes`.
2. Marks the stable memory region as freed or tombstoned.
3. Automatically removes any upgrade path entries that reference the deleted WASM hash, preventing re-introduction via `insert_upgrade_path_entries`. [7](#0-6) 

### Proof of Concept
1. NNS governance passes a proposal via `NnsFunction::AddSnsWasm` to add a new SNS governance WASM (e.g., version `v1.2.3`).
2. `add_wasm` writes the binary to stable memory and inserts the hash into `wasm_indexes`; `upgrade_path.add_wasm` sets it as the new `latest_version`.
3. SNS instances call `get_next_sns_version` and are directed to upgrade to `v1.2.3`.
4. A critical vulnerability is discovered in `v1.2.3`.
5. NNS governance passes `insert_upgrade_path_entries` to redirect the upgrade path to `v1.2.4`, bypassing `v1.2.3`.
6. However, `v1.2.3` remains permanently in `wasm_indexes` and stable memory — there is no `remove_wasm` call available.
7. SNS instances already running `v1.2.3` continue to do so; the vulnerable WASM binary remains retrievable via `get_wasm`; and a future `insert_upgrade_path_entries` proposal could inadvertently or maliciously re-introduce `v1.2.3` into an upgrade path since `wasm_indexes.contains_key(&v1_2_3_hash)` will always return `true`. [8](#0-7) [9](#0-8)

### Citations

**File:** rs/nns/sns-wasm/src/sns_wasm.rs (L90-113)
```rust
/// The struct that implements the public API of the canister
#[derive(Clone, Default)]
pub struct SnsWasmCanister<M: StableMemory + Clone + Default>
where
    SnsWasmCanister<M>: From<StableCanisterState>,
{
    /// A map from WASM hash to the index of this WASM in stable memory
    pub wasm_indexes: BTreeMap<[u8; 32], SnsWasmStableIndex>,
    /// Allowed subnets for SNSes to be installed
    pub sns_subnet_ids: Vec<SubnetId>,
    /// Stored deployed_sns instances
    pub deployed_sns_list: Vec<DeployedSns>,
    /// Specifies the upgrade path for SNS instances
    pub upgrade_path: UpgradePath,
    /// Provides convenient access to stable memory
    pub stable_memory: SnsWasmStableMemory<M>,
    /// If true, updates (e.g. add_wasm) can only be made by NNS Governance
    /// (via proposal execution), otherwise updates can be made by any caller
    pub access_controls_enabled: bool,
    /// List of principals that are allowed to deploy an SNS
    pub allowed_principals: Vec<PrincipalId>,
    /// Map of nns proposal id to index in the `deployed_sns_list`.
    pub nns_proposal_to_deployed_sns: BTreeMap<u64, u64>,
}
```

**File:** rs/nns/sns-wasm/src/sns_wasm.rs (L406-527)
```rust
    /// Adds a WASM to the canister's storage, validating that the expected hash matches that of the
    /// provided WASM bytecode.
    pub fn add_wasm(&mut self, add_wasm_payload: AddWasmRequest) -> AddWasmResponse {
        let AddWasmRequest {
            wasm,
            hash,
            skip_update_latest_version,
        } = add_wasm_payload;
        let wasm = wasm.expect("Wasm is required");

        let sns_canister_type = match wasm.checked_sns_canister_type() {
            Ok(canister_type) => canister_type,
            Err(message) => {
                println!(
                    "{}add_wasm invalid sns_canister_type: {}",
                    LOG_PREFIX, &message
                );

                return AddWasmResponse {
                    result: Some(add_wasm_response::Result::Error(SnsWasmError { message })),
                };
            }
        };

        let hash = vec_to_hash(hash).expect("Hash provided was not 32 bytes (i.e. [u8;32])");

        let skip_update_latest_version = skip_update_latest_version.unwrap_or(false);

        if hash != wasm.sha256_hash() {
            return AddWasmResponse {
                result: Some(add_wasm_response::Result::Error(SnsWasmError {
                    message: format!(
                        "Invalid Sha256 given for submitted WASM bytes. Provided hash was '{}' \
                         but calculated hash was '{}'",
                        hash_to_hex_string(&hash),
                        wasm.sha256_string()
                    ),
                })),
            };
        }

        let metadata = match Self::read_wasm_metadata_or_err(&wasm) {
            Ok(metadata) => metadata,
            Err(err) => {
                println!("err = {}, wasm = `{:?}`", err, wasm);
                return AddWasmResponse {
                    result: Some(add_wasm_response::Result::Error(SnsWasmError {
                        message: format!("Cannot read metadata sections from WASM: {err}"),
                    })),
                };
            }
        };

        let metadata = metadata
            .into_iter()
            .map(|metadata| {
                metadata
                    .validate()
                    .map(|_| MetadataSectionPb::from(metadata))
            })
            .collect::<Result<Vec<_>, _>>();

        let metadata = match metadata {
            Ok(metadata) => metadata,
            Err(err) => {
                return AddWasmResponse {
                    result: Some(add_wasm_response::Result::Error(SnsWasmError {
                        message: format!("Cannot validate metadata sections from WASM: {err}"),
                    })),
                };
            }
        };

        // Get the new latest version unless skip_update_latest_version is true.
        let new_latest_version = if skip_update_latest_version {
            None
        } else {
            // This function is fallible (as it checks for cycles in the upgrade path), but it has no side-effects.
            // So we want to try it first, and only if it succeeds, proceed to write the WASM to stable memory.
            let maybe_new_latest_version = self
                .upgrade_path
                .get_new_latest_version(sns_canister_type, &hash);

            match maybe_new_latest_version {
                Ok(new_latest_version) => Some(new_latest_version),
                Err(err) => {
                    return AddWasmResponse {
                        result: Some(add_wasm_response::Result::Error(SnsWasmError {
                            message: err,
                        })),
                    };
                }
            }
        };

        let result = match self.stable_memory.write_wasm(wasm) {
            Ok((offset, size)) => {
                self.wasm_indexes.insert(
                    hash,
                    SnsWasmStableIndex {
                        hash: hash.to_vec(),
                        offset,
                        size,
                        metadata,
                    },
                );

                if let Some(new_latest_version) = new_latest_version {
                    self.upgrade_path.add_wasm(new_latest_version);
                }

                add_wasm_response::Result::Hash(hash.to_vec())
            }
            Err(e) => {
                println!("{}add_wasm unable to persist WASM: {}", LOG_PREFIX, e);

                add_wasm_response::Result::Error(SnsWasmError {
                    message: format!("Unable to persist WASM: {e}"),
                })
            }
        };
        let result = Some(result);
```

**File:** rs/nns/sns-wasm/src/sns_wasm.rs (L580-608)
```rust
        // the upgrade request.
        for version in versions_submitted {
            let hash = match vec_to_hash(version) {
                Ok(h) => h,
                Err(e) => return InsertUpgradePathEntriesResponse::error(e),
            };
            if !self.wasm_indexes.contains_key(&hash) {
                return InsertUpgradePathEntriesResponse::error(
                    "Upgrade paths include WASM hashes that do not reference WASMs known by SNS-W"
                        .to_string(),
                );
            }
        }

        // Ensure the governance canister in the request belongs to a known SNS.
        if let Some(sns_governance_canister_id) = sns_governance_canister_id {
            // Note, if we ever get a substantial list here, we should make a data structure to
            // make this faster.
            if !self.deployed_sns_list.iter().any(|deployment| {
                deployment.governance_canister_id.is_some()
                    && deployment.governance_canister_id.unwrap()
                        == sns_governance_canister_id.into()
            }) {
                return InsertUpgradePathEntriesResponse::error(format!(
                    "Cannot add custom upgrade path for non-existent SNS.  Governance canister {sns_governance_canister_id} \
                     not found in list of deployed SNSes."
                ));
            }
        }
```

**File:** rs/nns/sns-wasm/src/sns_wasm.rs (L955-972)
```rust
        thread_safe_sns.with(|sns_canister| {
            sns_canister
                .borrow_mut()
                .deployed_sns_list
                .push(DeployedSns::from(sns_canisters));

            // Get the index of the DeployedSns we just pushed
            let latest_deployed_sns_index = sns_canister.borrow().deployed_sns_list.len() - 1;

            // Record the index in `nns_proposal_to_deployed_sns`
            sns_canister
                .borrow_mut()
                .nns_proposal_to_deployed_sns
                .insert(
                    sns_init_payload.nns_proposal_id(),
                    latest_deployed_sns_index as u64,
                );
        });
```

**File:** rs/nns/sns-wasm/canister/sns-wasm.did (L321-353)
```text
service : (SnsWasmCanisterInitPayload) -> {
  add_wasm : (AddWasmRequest) -> (AddWasmResponse);
  deploy_new_sns : (DeployNewSnsRequest) -> (DeployNewSnsResponse);
  get_allowed_principals : (record {}) -> (GetAllowedPrincipalsResponse) query;
  get_deployed_sns_by_proposal_id : (GetDeployedSnsByProposalIdRequest) -> (
      GetDeployedSnsByProposalIdResponse,
    ) query;
  get_latest_sns_version_pretty : (null) -> (vec record { text; text }) query;
  get_next_sns_version : (GetNextSnsVersionRequest) -> (
      GetNextSnsVersionResponse,
    ) query;
  get_proposal_id_that_added_wasm : (GetProposalIdThatAddedWasmRequest) -> (
      GetProposalIdThatAddedWasmResponse,
    ) query;
  get_sns_subnet_ids : (record {}) -> (GetSnsSubnetIdsResponse) query;
  get_wasm : (GetWasmRequest) -> (GetWasmResponse) query;
  get_wasm_metadata : (GetWasmMetadataRequest) -> (
      GetWasmMetadataResponse,
    ) query;
  insert_upgrade_path_entries : (InsertUpgradePathEntriesRequest) -> (
      InsertUpgradePathEntriesResponse,
    );
  list_deployed_snses : (record {}) -> (ListDeployedSnsesResponse) query;
  list_upgrade_steps : (ListUpgradeStepsRequest) -> (
      ListUpgradeStepsResponse,
    ) query;
  update_allowed_principals : (UpdateAllowedPrincipalsRequest) -> (
      UpdateAllowedPrincipalsResponse,
    );
  update_sns_subnet_list : (UpdateSnsSubnetListRequest) -> (
      UpdateSnsSubnetListResponse,
    );
}
```

**File:** rs/nns/governance/src/proposals/execute_nns_function.rs (L550-550)
```rust
            ValidNnsFunction::AddSnsWasm => (SNS_WASM_CANISTER_ID, "add_wasm"),
```

**File:** rs/nns/sns-wasm/canister/canister.rs (L384-395)
```rust
#[update]
fn update_allowed_principals(_: UpdateAllowedPrincipalsRequest) -> UpdateAllowedPrincipalsResponse {
    UpdateAllowedPrincipalsResponse {
        update_allowed_principals_result: Some(UpdateAllowedPrincipalsResult::Error(
            SnsWasmError {
                message: "update_allowed_principals is obsolete. For launching an SNS, please \
                          submit a CreateServiceNervousSystem proposal."
                    .to_string(),
            },
        )),
    }
}
```
