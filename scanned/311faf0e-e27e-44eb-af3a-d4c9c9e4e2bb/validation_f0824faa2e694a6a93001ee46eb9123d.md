### Title
Conditional Authorization Bypass in SNS-WASM Canister Allows Unprivileged Callers to Inject Arbitrary WASM and Manipulate SNS Upgrade Paths — (`File: rs/nns/sns-wasm/canister/canister.rs`)

---

### Summary

The SNS-WASM canister exposes two privileged `#[update]` methods — `add_wasm` and `insert_upgrade_path_entries` — whose authorization checks are gated behind a runtime boolean flag `access_controls_enabled`. When this flag is `false` (which is the **default value** in `SnsWasmCanisterInitPayloadBuilder`), any unprivileged ingress sender can call these methods directly, bypassing the NNS Governance requirement entirely. This is a direct analog to the ERC1967Factory bug where the admin check was absent, making privileged upgrade functions callable by anyone.

---

### Finding Description

In `rs/nns/sns-wasm/canister/canister.rs`, both `add_wasm` and `insert_upgrade_path_entries` use the following conditional authorization pattern:

```rust
// add_wasm (lines 301–310)
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
// insert_upgrade_path_entries (lines 312–325)
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

The condition `if access_controls_enabled && caller() != GOVERNANCE_CANISTER_ID.into()` means: **if `access_controls_enabled` is `false`, the entire caller check is short-circuited and skipped**, allowing any principal to proceed.

The `SnsWasmCanister` struct documents this explicitly:

```rust
/// If true, updates (e.g. add_wasm) can only be made by NNS Governance
/// (via proposal execution), otherwise updates can be made by any caller
pub access_controls_enabled: bool,
``` [3](#0-2) 

The default value of `access_controls_enabled` in `SnsWasmCanisterInitPayloadBuilder::new()` is `false`:

```rust
payload: SnsWasmCanisterInitPayload {
    sns_subnet_ids: vec![],
    access_controls_enabled: false,   // <-- default is false
    allowed_principals: vec![],
},
``` [4](#0-3) 

This flag is persisted in stable memory (`StableCanisterState.access_controls_enabled`) and restored on `post_upgrade` — meaning any deployment or upgrade that does not explicitly set `access_controls_enabled = true` will silently leave these privileged endpoints open to any caller. [5](#0-4) 

By contrast, `update_sns_subnet_list` uses an **unconditional** check with no flag dependency:

```rust
if caller() != GOVERNANCE_CANISTER_ID.into() {
    UpdateSnsSubnetListResponse::error(...)
}
``` [6](#0-5) 

This inconsistency confirms that `add_wasm` and `insert_upgrade_path_entries` have a weaker, bypassable authorization model.

---

### Impact Explanation

When `access_controls_enabled == false`, any unprivileged ingress sender can:

1. **Call `add_wasm`** to inject an arbitrary WASM binary (including malicious code) into the SNS-WASM canister's WASM store. The SNS-WASM canister is the authoritative source of WASM for all SNS deployments on the Internet Computer.

2. **Call `insert_upgrade_path_entries`** to manipulate the SNS upgrade path, pointing the `latest_version` or upgrade steps to the attacker-injected WASM hash.

The combined effect is that all future SNS deployments and all SNS instances that follow the standard upgrade path would install attacker-controlled code. This constitutes a **governance authorization bypass** with **chain-wide SNS compromise** impact — equivalent to the ERC1967Factory bug where anyone could change the implementation contract.

---

### Likelihood Explanation

The `access_controls_enabled` flag defaults to `false` in `SnsWasmCanisterInitPayloadBuilder::new()`. Any deployment, test environment, or canister upgrade that does not explicitly call `.with_access_controls_enabled(true)` will have these endpoints open. The test `test_add_wasm_can_be_called_directly_if_access_controls_are_disabled` explicitly confirms the bypass is reachable and functional. The flag is a runtime state variable with no enforcement that it must be `true` in production, making misconfiguration a realistic risk. [7](#0-6) 

---

### Recommendation

Replace the conditional authorization pattern in `add_wasm` and `insert_upgrade_path_entries` with an **unconditional** caller check, matching the pattern already used in `update_sns_subnet_list`:

```rust
// Recommended fix for add_wasm
#[update]
fn add_wasm(add_wasm_payload: AddWasmRequest) -> AddWasmResponse {
    if caller() != GOVERNANCE_CANISTER_ID.into() {
        return AddWasmResponse::error("add_wasm can only be called by NNS Governance".into());
    }
    SNS_WASM.with(|sns_wasm| sns_wasm.borrow_mut().add_wasm(add_wasm_payload))
}
```

The `access_controls_enabled` flag and its associated bypass path should be removed entirely. If a testing escape hatch is needed, it should be enforced at the test harness level (e.g., via `#[cfg(test)]`), not as a runtime-configurable flag in production canister logic.

---

### Proof of Concept

The following test already exists in the codebase and directly demonstrates the bypass:

```rust
#[test]
fn test_add_wasm_can_be_called_directly_if_access_controls_are_disabled() {
    let machine = StateMachine::new();

    let nns_init_payload = NnsInitPayloadsBuilder::new()
        .with_sns_dedicated_subnets(machine.get_subnet_ids())
        .with_sns_wasm_access_controls(false)   // access_controls_enabled = false
        .build();

    let sns_wasm_canister_id = install_sns_wasm(&machine, &nns_init_payload);

    let root_wasm = build_root_sns_wasm();
    let root_hash = root_wasm.sha256_hash();
    let response = add_wasm(&machine, sns_wasm_canister_id, root_wasm, &root_hash);

    // Any caller succeeds — no governance proposal required
    assert_eq!(
        response.result.unwrap(),
        add_wasm_response::Result::Hash(root_hash.to_vec())
    );
}
``` [7](#0-6) 

An attacker would:
1. Craft a malicious SNS governance/root/ledger WASM.
2. Call `add_wasm` directly (no governance proposal) with the malicious WASM bytes.
3. Call `insert_upgrade_path_entries` to set the malicious WASM hash as the next upgrade step.
4. All SNS instances following the standard upgrade path will now install the attacker's WASM on their next upgrade cycle.

### Citations

**File:** rs/nns/sns-wasm/canister/canister.rs (L290-299)
```rust
#[post_upgrade]
fn canister_post_upgrade() {
    println!("{}Executing post upgrade", LOG_PREFIX);

    SNS_WASM.with(|c| {
        c.replace(SnsWasmCanister::<CanisterStableMemory>::from_stable_memory());
    });

    println!("{}Completed post upgrade", LOG_PREFIX);
}
```

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

**File:** rs/nns/sns-wasm/canister/canister.rs (L406-414)
```rust
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

**File:** rs/nns/sns-wasm/src/sns_wasm.rs (L106-108)
```rust
    /// If true, updates (e.g. add_wasm) can only be made by NNS Governance
    /// (via proposal execution), otherwise updates can be made by any caller
    pub access_controls_enabled: bool,
```

**File:** rs/nns/sns-wasm/src/init.rs (L18-23)
```rust
            payload: SnsWasmCanisterInitPayload {
                sns_subnet_ids: vec![],
                access_controls_enabled: false,
                allowed_principals: vec![],
            },
        }
```

**File:** rs/nns/sns-wasm/tests/add_wasm.rs (L47-65)
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
```
