### Title
Unprotected WASM Store Allows Arbitrary Module Injection When `access_controls_enabled` Defaults to `false` - (File: `rs/nns/sns-wasm/canister/canister.rs`)

---

### Summary

The SNS-WASM canister's `add_wasm` and `insert_upgrade_path_entries` update methods bypass all caller authentication when the `access_controls_enabled` flag is `false`. The `SnsWasmCanisterInitPayloadBuilder` defaults this flag to `false`, meaning any deployment that omits an explicit `true` setting leaves the entire WASM store open to arbitrary writes by any unprivileged ingress sender. This is a direct structural analog to the Rocket Pool unprotected-storage-during-deployment bug: both gate access control on a boolean flag that defaults to the unprotected state.

---

### Finding Description

In `rs/nns/sns-wasm/canister/canister.rs`, the `add_wasm` and `insert_upgrade_path_entries` update methods share the same conditional guard:

```rust
// add_wasm
if access_controls_enabled && caller() != GOVERNANCE_CANISTER_ID.into() {
    AddWasmResponse::error("add_wasm can only be called by NNS Governance".into())
} else {
    SNS_WASM.with(|sns_wasm| sns_wasm.borrow_mut().add_wasm(add_wasm_payload))
}
``` [1](#0-0) 

When `access_controls_enabled` is `false`, the `else` branch executes unconditionally for **any** caller — anonymous, user, or canister. The same pattern applies to `insert_upgrade_path_entries`: [2](#0-1) 

The flag is sourced from the init payload and persisted to stable memory. The `SnsWasmCanisterInitPayloadBuilder::new()` in `rs/nns/sns-wasm/src/init.rs` hard-codes the default to `false`:

```rust
payload: SnsWasmCanisterInitPayload {
    sns_subnet_ids: vec![],
    access_controls_enabled: false,   // <-- insecure default
    allowed_principals: vec![],
},
``` [3](#0-2) 

The `post_upgrade` hook restores state from stable memory rather than from a fresh init payload, so the flag persists across all future upgrades once set: [4](#0-3) 

The field is documented explicitly as an intentional bypass:

> "If true, updates (e.g. add_wasm) can only be made by NNS Governance (via proposal execution), **otherwise updates can be made by any caller**" [5](#0-4) 

The PocketIC NNS setup path uses the builder without setting the flag to `true`:

```rust
let sns_wasm_init_payload = SnsWasmCanisterInitPayloadBuilder::new()
    .with_sns_subnet_ids(vec![sns_subnet_id])
    .build();   // access_controls_enabled stays false
``` [6](#0-5) 

The vulnerability is confirmed by a dedicated test that explicitly verifies any caller succeeds when the flag is disabled: [7](#0-6) 

---

### Impact Explanation

An unprivileged ingress sender targeting a deployment where `access_controls_enabled = false` can:

1. **Inject backdoored WASM modules** — call `add_wasm` with a malicious SNS governance, ledger, swap, root, or index WASM. The hash is stored and the module becomes the canonical version used by `deploy_new_sns`.
2. **Poison the upgrade path** — call `insert_upgrade_path_entries` to insert a transition that forces all existing SNS instances to upgrade to attacker-controlled code.

Any SNS deployed or upgraded via the compromised SNS-WASM canister executes attacker-controlled code, enabling: SNS treasury drain, governance takeover, ledger mint/burn manipulation, or permanent denial of SNS functionality.

---

### Likelihood Explanation

The `SnsWasmCanisterInitPayloadBuilder::new()` default is `false`. Any deployment that does not explicitly call `.with_access_controls_enabled(true)` is vulnerable. The PocketIC NNS bootstrap path omits this call. Staging or developer environments bootstrapped from PocketIC helpers and later promoted to production, or new subnet deployments using the builder directly, would carry the insecure default. The mainnet NNS SNS-WASM canister (`qaa6y-5yaaa-aaaaa-aaafa-cai`) was deployed with `access_controls_enabled = true`, but the code path to a vulnerable deployment is one omitted builder call away.

---

### Recommendation

1. **Change the default** in `SnsWasmCanisterInitPayloadBuilder::new()` to `access_controls_enabled: true`. Testing environments that need the flag disabled should opt out explicitly.
2. **Add a canister-init assertion** that panics if `access_controls_enabled` is `false` in non-test builds (gated on a `#[cfg(not(feature = "test"))]` guard).
3. **Consider removing the flag entirely** and always requiring NNS Governance authorization for `add_wasm` and `insert_upgrade_path_entries`, using a separate test-only canister variant for integration tests.

---

### Proof of Concept

```
1. Deploy SNS-WASM using SnsWasmCanisterInitPayloadBuilder::new()
   (omit .with_access_controls_enabled(true))
   → access_controls_enabled = false persisted to stable memory

2. As any unprivileged principal P (ingress):
   call add_wasm({
     wasm: <backdoored_sns_governance_wasm>,
     hash: sha256(<backdoored_sns_governance_wasm>),
     canister_type: SNS_CANISTER_TYPE_GOVERNANCE,
   })
   → Returns Hash(...), no authorization check performed

3. call insert_upgrade_path_entries({
     upgrade_path: [{ current_version: <current>, next_version: <backdoored> }],
   })
   → Upgrade path poisoned for all existing SNS instances

4. Any subsequent deploy_new_sns installs the backdoored governance WASM.
   Any existing SNS that follows the upgrade path installs the backdoored WASM.
   Attacker now controls SNS governance → drains treasury, mints tokens, etc.
```

This exact flow is exercised (without the malicious payload) by `test_add_wasm_can_be_called_directly_if_access_controls_are_disabled` and `insert_upgrade_path_entries_callable_by_anyone_when_access_controls_disabled`. [8](#0-7)

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

**File:** rs/nns/sns-wasm/src/init.rs (L16-24)
```rust
    pub fn new() -> Self {
        SnsWasmCanisterInitPayloadBuilder {
            payload: SnsWasmCanisterInitPayload {
                sns_subnet_ids: vec![],
                access_controls_enabled: false,
                allowed_principals: vec![],
            },
        }
    }
```

**File:** rs/nns/sns-wasm/src/sns_wasm.rs (L106-108)
```rust
    /// If true, updates (e.g. add_wasm) can only be made by NNS Governance
    /// (via proposal execution), otherwise updates can be made by any caller
    pub access_controls_enabled: bool,
```

**File:** rs/pocket_ic_server/src/pocket_ic.rs (L1893-1895)
```rust
            let sns_wasm_init_payload = SnsWasmCanisterInitPayloadBuilder::new()
                .with_sns_subnet_ids(vec![sns_subnet_id])
                .build();
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
