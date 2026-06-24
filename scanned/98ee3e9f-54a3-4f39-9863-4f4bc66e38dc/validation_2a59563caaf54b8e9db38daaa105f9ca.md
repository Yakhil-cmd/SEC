### Title
Governance Authorization Bypass via Disabled Access Controls in SNS-WASM Canister - (File: rs/nns/sns-wasm/canister/canister.rs)

---

### Summary

The SNS-WASM canister (`rs/nns/sns-wasm/canister/canister.rs`) contains a persistent, runtime-configurable flag `access_controls_enabled` that, when set to `false`, allows **any unprivileged ingress sender or canister caller** to invoke `add_wasm` and `insert_upgrade_path_entries` without NNS Governance authorization. The default value of this flag in `SnsWasmCanisterInitPayloadBuilder::new()` is `false`. This is the direct IC analog of the ERC1538 proxy trust issue: the system's upgrade path and WASM registry can be manipulated by an unauthorized actor, stripping the SNS ecosystem of its trustless nature.

---

### Finding Description

The `add_wasm` and `insert_upgrade_path_entries` update methods in the SNS-WASM canister gate their authorization check behind a runtime boolean flag `access_controls_enabled`:

```rust
// rs/nns/sns-wasm/canister/canister.rs:301-325
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

The default value of `access_controls_enabled` in `SnsWasmCanisterInitPayloadBuilder::new()` is `false`:

```rust
// rs/nns/sns-wasm/src/init.rs:16-23
payload: SnsWasmCanisterInitPayload {
    sns_subnet_ids: vec![],
    access_controls_enabled: false,  // <-- insecure default
    allowed_principals: vec![],
},
```

The production NNS initialization path in `rs/nns/init/src/main.rs` calls `init_payloads_builder.build()` without ever calling `.with_sns_wasm_access_controls(true)`, meaning the SNS-WASM canister is deployed with `access_controls_enabled = false` unless the deployer explicitly overrides it.

When `access_controls_enabled` is `false`:
- Any principal (anonymous, user, or canister) can call `add_wasm` to register arbitrary WASM binaries into the SNS-WASM store.
- Any principal can call `insert_upgrade_path_entries` to rewrite the SNS upgrade path, redirecting all future SNS upgrades to attacker-controlled WASM hashes.

The `access_controls_enabled` flag is persisted to stable memory and survives upgrades via `pre_upgrade`/`post_upgrade`, so a deployment with `false` remains permanently open unless explicitly changed via a governance proposal.

---

### Impact Explanation

**Governance authorization bug** — an unprivileged ingress sender can:

1. Call `add_wasm` with a malicious WASM binary (e.g., a backdoored SNS governance canister). The SNS-WASM store accepts it without any caller check.
2. Call `insert_upgrade_path_entries` to insert an upgrade path entry pointing from the current legitimate SNS version to the malicious WASM hash.
3. All deployed SNS instances that run their periodic upgrade task (`advance_target_sns_version`) will now automatically upgrade to the attacker-controlled WASM, giving the attacker full control over every SNS governance, root, ledger, swap, and index canister on the IC.

This is a complete compromise of the SNS ecosystem's trustless upgrade mechanism — equivalent to the ERC1538 proxy owner silently swapping all delegate implementations.

---

### Likelihood Explanation

The `SnsWasmCanisterInitPayloadBuilder::new()` default is `false`. The production NNS init tool (`rs/nns/init/src/main.rs`) does not call `.with_sns_wasm_access_controls(true)`. If the mainnet SNS-WASM canister was deployed using this builder without an explicit override, access controls are disabled and the attack is immediately reachable by any ingress sender with no special privileges. The attack requires only knowledge of the Candid interface, which is public.

---

### Recommendation

1. **Change the default** of `access_controls_enabled` in `SnsWasmCanisterInitPayloadBuilder::new()` from `false` to `true`.
2. **Remove the flag entirely** in production — the authorization check should be unconditional, not runtime-configurable.
3. If the flag must remain for testing purposes, ensure it is only settable at init time and cannot be `false` in any production deployment path.
4. Audit the production deployment scripts and NNS init payloads to confirm `access_controls_enabled = true` is enforced.

---

### Proof of Concept

**Attacker entry path (unprivileged ingress):**

1. Observe that `access_controls_enabled` defaults to `false` in `SnsWasmCanisterInitPayloadBuilder::new()`: [1](#0-0) 

2. Confirm the production NNS init path does not set `access_controls_enabled = true`: [2](#0-1) 

3. Observe the authorization bypass in `add_wasm` — the check is skipped when `access_controls_enabled` is `false`: [3](#0-2) 

4. Observe the same bypass in `insert_upgrade_path_entries`: [4](#0-3) 

5. The `access_controls_enabled` field is persisted to stable memory and survives upgrades: [5](#0-4) 

6. The existing test explicitly confirms that when `access_controls_enabled = false`, any caller can invoke `insert_upgrade_path_entries`: [6](#0-5) 

7. The `insert_upgrade_path_entries` logic, once past the access control gate, directly rewrites the SNS upgrade path used by all deployed SNS instances: [7](#0-6) 

**Attack sequence:**
```
Attacker → add_wasm(malicious_wasm)          [no auth check when flag=false]
Attacker → insert_upgrade_path_entries(      [no auth check when flag=false]
    current_version → malicious_wasm_hash
)
SNS periodic task → advance_target_sns_version → upgrades to malicious WASM
```

### Citations

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

**File:** rs/nns/init/src/main.rs (L175-254)
```rust
/// Constructs the `NnsInitPayloads` from the command line options.
fn create_init_payloads(args: &CliArgs) -> NnsInitPayloads {
    let mut init_payloads_builder = NnsInitPayloadsBuilder::new();

    add_registry_content(
        &mut init_payloads_builder,
        args.initial_registry.as_ref(),
        args.registry_local_store_dir.as_ref(),
    );

    if let Some(path) = &args.initial_neurons {
        eprintln!("{LOG_PREFIX}Initializing neurons from CSV file: {path:?}");
        init_payloads_builder.with_neurons_from_csv_file(path);
    } else {
        eprintln!(
            "{LOG_PREFIX}Initial neuron CSV or PB path not specified, initializing with test neurons"
        );
        init_payloads_builder
            // We need some neurons, because we need to vote on some proposals to create subnets.
            .with_test_neurons();
    }

    let mut test_ledger_accounts = vec![];

    for principal in &args.initialize_ledger_with_test_accounts_for_principals {
        test_ledger_accounts.push(icp_ledger::AccountIdentifier::new(*principal, None));
    }
    for account_hex in &args.initialize_ledger_with_test_accounts {
        test_ledger_accounts.push(
            icp_ledger::AccountIdentifier::from_hex(account_hex)
                .expect("failed to parse ledger account identifier"),
        );
    }

    for account in test_ledger_accounts.into_iter() {
        init_payloads_builder
            .ledger
            .init_args()
            .unwrap()
            .initial_values
            .insert(
                account,
                icp_ledger::Tokens::from_tokens(1_000_000_000).expect("Couldn't create icpts"),
            );
        eprintln!(
            "{}Initializing with test ledger account: {}",
            LOG_PREFIX,
            account.to_hex(),
        );
    }

    if args.initialize_with_gtc_neurons {
        init_payloads_builder.genesis_token.sr_months_to_release =
            args.months_to_release_seed_round_gtc_neurons;
        init_payloads_builder.genesis_token.ect_months_to_release =
            args.months_to_release_ect_gtc_neurons;

        init_payloads_builder.with_gtc_neurons();
    }

    init_payloads_builder
        .genesis_token
        .donate_account_recipient_neuron_id = Some(GTC_DONATE_ACCOUNT_RECIPIENT_NEURON_ID);

    init_payloads_builder
        .genesis_token
        .forward_whitelisted_unclaimed_accounts_recipient_neuron_id =
        Some(GTC_FORWARD_ALL_UNCLAIMED_ACCOUNTS_RECIPIENT_NEURON_ID);

    init_payloads_builder.sns_wasms.with_sns_subnet_ids(
        args.sns_subnet
            .iter()
            .cloned()
            .map(SubnetId::from)
            .collect(),
    );

    println!("{LOG_PREFIX}Initialized governance.");

    init_payloads_builder.build()
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

**File:** rs/nns/sns-wasm/src/gen/ic_sns_wasm.pb.v1.rs (L7-22)
```rust
pub struct StableCanisterState {
    #[prost(message, repeated, tag = "1")]
    pub wasm_indexes: ::prost::alloc::vec::Vec<SnsWasmStableIndex>,
    #[prost(message, repeated, tag = "2")]
    pub sns_subnet_ids: ::prost::alloc::vec::Vec<::ic_base_types::PrincipalId>,
    #[prost(message, repeated, tag = "3")]
    pub deployed_sns_list: ::prost::alloc::vec::Vec<DeployedSns>,
    #[prost(message, optional, tag = "4")]
    pub upgrade_path: ::core::option::Option<UpgradePath>,
    #[prost(bool, tag = "5")]
    pub access_controls_enabled: bool,
    #[prost(message, repeated, tag = "6")]
    pub allowed_principals: ::prost::alloc::vec::Vec<::ic_base_types::PrincipalId>,
    #[prost(btree_map = "uint64, uint64", tag = "7")]
    pub nns_proposal_to_deployed_sns: ::prost::alloc::collections::BTreeMap<u64, u64>,
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
