### Title
Governance Authorization Bypass via Conditional Access Control in SNS-WASM `add_wasm` and `insert_upgrade_path_entries` - (File: rs/nns/sns-wasm/canister/canister.rs)

### Summary
The SNS-WASM canister's `add_wasm` and `insert_upgrade_path_entries` update methods gate their access control on a runtime boolean flag `access_controls_enabled`. When this flag is `false` — which is the **default** value in `SnsWasmCanisterInitPayloadBuilder::new()` — any unprivileged ingress sender or canister caller can invoke these privileged functions without restriction, bypassing the intended NNS Governance-only authorization.

### Finding Description
Both `add_wasm` and `insert_upgrade_path_entries` in `rs/nns/sns-wasm/canister/canister.rs` implement access control as a conditional branch rather than an unconditional guard:

```rust
#[update]
fn add_wasm(add_wasm_payload: AddWasmRequest) -> AddWasmResponse {
    let access_controls_enabled =
        SNS_WASM.with(|sns_wasm| sns_wasm.borrow().access_controls_enabled);
    if access_controls_enabled && caller() != GOVERNANCE_CANISTER_ID.into() {
        AddWasmResponse::error("add_wasm can only be called by NNS Governance".into())
    } else {
        SNS_WASM.with(|sns_wasm| sns_wasm.borrow_mut().add_wasm(add_wasm_payload))
    }
}
``` [1](#0-0) 

```rust
#[update]
fn insert_upgrade_path_entries(
    payload: InsertUpgradePathEntriesRequest,
) -> InsertUpgradePathEntriesResponse {
    let access_controls_enabled =
        SNS_WASM.with(|sns_wasm| sns_wasm.borrow().access_controls_enabled);
    if access_controls_enabled && caller() != GOVERNANCE_CANISTER_ID.into() {
        InsertUpgradePathEntriesResponse::error(
            "insert_upgrade_path_entries can only be called by NNS Governance".into(),
        )
    } else {
        SNS_WASM.with(|sns_wasm| sns_wasm.borrow_mut().insert_upgrade_path_entries(payload))
    }
}
``` [2](#0-1) 

The `access_controls_enabled` field defaults to `false` in `SnsWasmCanisterInitPayloadBuilder::new()`:

```rust
payload: SnsWasmCanisterInitPayload {
    sns_subnet_ids: vec![],
    access_controls_enabled: false,   // insecure default
    allowed_principals: vec![],
},
``` [3](#0-2) 

The field is documented in the canister struct:

```rust
/// If true, updates (e.g. add_wasm) can only be made by NNS Governance
/// (via proposal execution), otherwise updates can be made by any caller
pub access_controls_enabled: bool,
``` [4](#0-3) 

The codebase itself contains a test that explicitly confirms any caller can invoke `insert_upgrade_path_entries` when access controls are disabled: [5](#0-4) 

### Impact Explanation
When `access_controls_enabled = false`, any unprivileged ingress sender or canister can:

1. **`add_wasm`**: Inject arbitrary WASM binaries into the SNS-WASM canister's trusted store. These WASMs are used as the authoritative source for SNS canister upgrades across all deployed SNS instances on the IC.
2. **`insert_upgrade_path_entries`**: Rewrite the SNS upgrade path, redirecting SNS governance, ledger, root, swap, or archive canisters to upgrade to attacker-controlled WASMs.

The combined effect is a full supply-chain compromise of every SNS instance: an attacker could cause all SNS canisters to upgrade to malicious code, enabling theft of SNS treasury funds, neuron manipulation, or complete takeover of SNS-governed dapps. [6](#0-5) 

### Likelihood Explanation
The production NNS deployment appears to initialize SNS-WASM with `access_controls_enabled = true` (evidenced by `test_add_wasm_cannot_be_called_directly` passing against the standard NNS setup). However, the risk is real in two scenarios:

1. **Direct deployment using `SnsWasmCanisterInitPayloadBuilder::new()`** without explicitly calling `with_access_controls_enabled(true)` — the default is `false`, leaving the canister open.
2. **Canister reinstall** via a governance proposal using a payload that omits or defaults `access_controls_enabled` — this would silently disable access controls on the live production canister.

The `update_sns_subnet_list` function, by contrast, uses an unconditional check (`if caller() != GOVERNANCE_CANISTER_ID.into()`), demonstrating that the conditional pattern in `add_wasm`/`insert_upgrade_path_entries` is an inconsistency rather than intentional design parity. [7](#0-6) 

### Recommendation
Replace the conditional access control with an unconditional guard, matching the pattern used by `update_sns_subnet_list`:

```rust
#[update]
fn add_wasm(add_wasm_payload: AddWasmRequest) -> AddWasmResponse {
    if caller() != GOVERNANCE_CANISTER_ID.into() {
        return AddWasmResponse::error("add_wasm can only be called by NNS Governance".into());
    }
    SNS_WASM.with(|sns_wasm| sns_wasm.borrow_mut().add_wasm(add_wasm_payload))
}
```

The `access_controls_enabled` flag and `SnsWasmCanisterInitPayloadBuilder` default should be removed or changed to `true`. If a testing escape hatch is required, it must be enforced at the build level (e.g., `#[cfg(test)]`) rather than as a runtime-configurable production flag.

### Proof of Concept
1. Deploy SNS-WASM canister using `SnsWasmCanisterInitPayloadBuilder::new().build()` (default `access_controls_enabled = false`).
2. As any principal (including anonymous), call `add_wasm` with a malicious WASM binary and its SHA-256 hash.
3. Call `insert_upgrade_path_entries` to set the upgrade path from the current SNS version to the malicious WASM version.
4. Any SNS instance that subsequently triggers an upgrade will install the attacker-supplied WASM.

The test at `rs/nns/sns-wasm/tests/upgrade_sns_instance.rs:1022–1054` (`insert_upgrade_path_entries_callable_by_anyone_when_access_controls_disabled`) confirms step 2–3 succeed for any caller when `access_controls_enabled = false`. [5](#0-4) [8](#0-7)

### Citations

**File:** rs/nns/sns-wasm/canister/canister.rs (L301-310)
```rust
#[update]
fn add_wasm(add_wasm_payload: AddWasmRequest) -> AddWasmResponse {
    let access_controls_enabled =
        SNS_WASM.with(|sns_wasm| sns_wasm.borrow().access_controls_enabled);
    if access_controls_enabled && caller() != GOVERNANCE_CANISTER_ID.into() {
        AddWasmResponse::error("add_wasm can only be called by NNS Governance".into())
    } else {
        SNS_WASM.with(|sns_wasm| sns_wasm.borrow_mut().add_wasm(add_wasm_payload))
    }
}
```

**File:** rs/nns/sns-wasm/canister/canister.rs (L312-325)
```rust
#[update]
fn insert_upgrade_path_entries(
    payload: InsertUpgradePathEntriesRequest,
) -> InsertUpgradePathEntriesResponse {
    let access_controls_enabled =
        SNS_WASM.with(|sns_wasm| sns_wasm.borrow().access_controls_enabled);
    if access_controls_enabled && caller() != GOVERNANCE_CANISTER_ID.into() {
        InsertUpgradePathEntriesResponse::error(
            "insert_upgrade_path_entries can only be called by NNS Governance".into(),
        )
    } else {
        SNS_WASM.with(|sns_wasm| sns_wasm.borrow_mut().insert_upgrade_path_entries(payload))
    }
}
```

**File:** rs/nns/sns-wasm/canister/canister.rs (L404-414)
```rust
/// Add or remove SNS subnet IDs from the list of subnet IDs that SNS instances will be deployed to
#[update]
fn update_sns_subnet_list(request: UpdateSnsSubnetListRequest) -> UpdateSnsSubnetListResponse {
    if caller() != GOVERNANCE_CANISTER_ID.into() {
        UpdateSnsSubnetListResponse::error(
            "update_sns_subnet_list can only be called by NNS Governance",
        )
    } else {
        SNS_WASM.with(|sns_wasm| sns_wasm.borrow_mut().update_sns_subnet_list(request))
    }
}
```

**File:** rs/nns/sns-wasm/src/init.rs (L16-23)
```rust
    pub fn new() -> Self {
        SnsWasmCanisterInitPayloadBuilder {
            payload: SnsWasmCanisterInitPayload {
                sns_subnet_ids: vec![],
                access_controls_enabled: false,
                allowed_principals: vec![],
            },
        }
```

**File:** rs/nns/sns-wasm/src/sns_wasm.rs (L106-108)
```rust
    /// If true, updates (e.g. add_wasm) can only be made by NNS Governance
    /// (via proposal execution), otherwise updates can be made by any caller
    pub access_controls_enabled: bool,
```

**File:** rs/nns/sns-wasm/src/sns_wasm.rs (L532-627)
```rust
    /// Insert upgrade path entries for the general path or for an SNS-specific path.
    pub fn insert_upgrade_path_entries(
        &mut self,
        request: InsertUpgradePathEntriesRequest,
    ) -> InsertUpgradePathEntriesResponse {
        let InsertUpgradePathEntriesRequest {
            upgrade_path,
            sns_governance_canister_id,
        } = request;

        let sns_governance_canister_id = match sns_governance_canister_id {
            None => None,
            Some(id) => match CanisterId::try_from(id) {
                Ok(canister_id) => Some(canister_id),
                Err(_) => {
                    return InsertUpgradePathEntriesResponse::error(format!(
                        "Request.sns_governance_canister_id ({id}) \
                        could not be converted to a canister ID"
                    ));
                }
            },
        };

        if upgrade_path.is_empty() {
            return InsertUpgradePathEntriesResponse::error(
                "No Upgrade Paths in request. No action taken.".to_string(),
            );
        }

        let mut versions_submitted = vec![];
        for upgrade_step in &upgrade_path {
            let SnsUpgrade {
                current_version,
                next_version,
            } = upgrade_step.clone();

            if current_version.is_none() || next_version.is_none() {
                return InsertUpgradePathEntriesResponse::error(
                    "A provided SnsUpgrade entry does not have a current_version or next_version"
                        .to_string(),
                );
            }
            versions_submitted.append(&mut current_version.unwrap().version_hashes());
            versions_submitted.append(&mut next_version.unwrap().version_hashes());
        }
        let versions_submitted: HashSet<Vec<u8>> = versions_submitted.into_iter().collect();

        // Ensure we have the WASMs in the submitted versions, otherwise the SNS could not execute
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

**File:** rs/nns/sns-wasm/tests/upgrade_sns_instance.rs (L1022-1054)
```rust
#[test]
fn insert_upgrade_path_entries_callable_by_anyone_when_access_controls_disabled() {
    let machine = StateMachineBuilder::new().with_current_time().build();

    let nns_init_payload = NnsInitPayloadsBuilder::new()
        .with_sns_wasm_access_controls(false)
        .with_initial_invariant_compliant_mutations()
        .with_test_neurons()
        .with_sns_dedicated_subnets(machine.get_subnet_ids())
        .build();

    setup_nns_canisters(&machine, nns_init_payload);

    let response: InsertUpgradePathEntriesResponse = update_with_sender(
        &machine,
        SNS_WASM_CANISTER_ID,
        "insert_upgrade_path_entries",
        InsertUpgradePathEntriesRequest {
            upgrade_path: vec![],
            sns_governance_canister_id: None,
        },
        PrincipalId::new_user_test_id(1),
    )
    .unwrap();

    // We get an error past the access controls (request was actually processed)
    assert_eq!(
        response,
        InsertUpgradePathEntriesResponse::error(
            "No Upgrade Paths in request. No action taken.".to_string()
        )
    );
}
```

**File:** rs/nns/sns-wasm/tests/add_wasm.rs (L47-66)
```rust
#[test]
fn test_add_wasm_can_be_called_directly_if_access_controls_are_disabled() {
    let machine = StateMachine::new();

    let nns_init_payload = NnsInitPayloadsBuilder::new()
        .with_sns_dedicated_subnets(machine.get_subnet_ids())
        .with_sns_wasm_access_controls(false)
        .build();

    let sns_wasm_canister_id = install_sns_wasm(&machine, &nns_init_payload);

    let root_wasm = build_root_sns_wasm();
    let root_hash = root_wasm.sha256_hash();
    let response = add_wasm(&machine, sns_wasm_canister_id, root_wasm, &root_hash);

    assert_eq!(
        response.result.unwrap(),
        add_wasm_response::Result::Hash(root_hash.to_vec())
    );
}
```
