The code evidence is conclusive. Let me trace every relevant piece:

**1. The default is confirmed `false`:** [1](#0-0) 

**2. The guard is a conditional, not an unconditional check:** [2](#0-1) [3](#0-2) 

**3. The production deployment tool (`rs/nns/init/src/main.rs`) never calls `with_sns_wasm_access_controls`:** [4](#0-3) 

The `NnsInitPayloadsBuilder::new()` embeds `SnsWasmCanisterInitPayloadBuilder::new()` directly, inheriting the `false` default: [5](#0-4) 

**4. The `access_controls_enabled` value is persisted to stable memory and restored on every upgrade — it is never re-derived from a safe default:** [6](#0-5) [7](#0-6) 

**5. The exploit is already proven by existing tests in the repo:** [8](#0-7) [9](#0-8) 

---

### Title
Insecure-by-default `access_controls_enabled: false` in `SnsWasmCanisterInitPayloadBuilder` allows any unprivileged principal to poison the SNS upgrade path — (`rs/nns/sns-wasm/src/init.rs`)

### Summary
`SnsWasmCanisterInitPayloadBuilder::new()` defaults `access_controls_enabled` to `false`. The authorization guards in `add_wasm` and `insert_upgrade_path_entries` are conditional on this flag. The production NNS deployment tool (`rs/nns/init/src/main.rs`) never overrides this default. Any deployment or re-installation of the SNS-WASM canister using the builder without explicitly calling `.with_access_controls_enabled(true)` leaves both mutation endpoints open to any caller.

### Finding Description
In `rs/nns/sns-wasm/src/init.rs`, `SnsWasmCanisterInitPayloadBuilder::new()` initializes `access_controls_enabled: false`. In `rs/nns/sns-wasm/canister/canister.rs`, both `add_wasm` (line 305) and `insert_upgrade_path_entries` (line 318) gate the NNS Governance check with `if access_controls_enabled && caller() != GOVERNANCE_CANISTER_ID`. When the flag is `false`, the `if` branch is never entered and any caller proceeds to the mutation logic unconditionally.

The production deployment CLI (`rs/nns/init/src/main.rs`, `create_init_payloads`) constructs `NnsInitPayloadsBuilder::new()` which embeds `SnsWasmCanisterInitPayloadBuilder::new()` and never calls `with_access_controls_enabled(true)`. The `access_controls_enabled` value is serialized into `StableCanisterState` on `pre_upgrade` and deserialized on `post_upgrade`, so whatever value was set at `canister_init` time persists indefinitely through all future upgrades.

### Impact Explanation
With `access_controls_enabled: false`, any unprivileged principal can:
1. Call `add_wasm` to register an attacker-controlled WASM binary for any `SnsCanisterType` (e.g., `Governance`).
2. Call `insert_upgrade_path_entries` to wire that malicious hash into the canonical SNS upgrade path.
3. Every subsequent `deploy_new_sns` call will install the attacker's WASM into newly created SNS governance canisters, giving the attacker full control over all future SNS deployments.

### Likelihood Explanation
The production deployment tool does not set `access_controls_enabled: true`. If the mainnet SNS-WASM canister was initialized using this tool (or any path that uses the builder default), it is currently running with `access_controls_enabled: false` and the endpoints are open to any caller right now. Even if the current mainnet state happens to have `true` (e.g., set via a separate re-installation), any future re-installation using the default builder would re-open the attack surface. The existing test suite explicitly proves the open-access behavior is reachable (`test_add_wasm_can_be_called_directly_if_access_controls_are_disabled`, `insert_upgrade_path_entries_callable_by_anyone_when_access_controls_disabled`).

### Recommendation
1. Change the default in `SnsWasmCanisterInitPayloadBuilder::new()` to `access_controls_enabled: true`.
2. Remove the conditional guard entirely: `add_wasm` and `insert_upgrade_path_entries` should unconditionally require `caller() == GOVERNANCE_CANISTER_ID`, with no runtime bypass.
3. Update `rs/nns/init/src/main.rs` to explicitly set `access_controls_enabled: true` regardless of the builder default.
4. Add a `canister_init` assertion that panics if `access_controls_enabled` is `false` in a non-test build.

### Proof of Concept
```rust
// State-machine test (mirrors existing test structure):
let machine = StateMachine::new();
let nns_init_payload = NnsInitPayloadsBuilder::new()  // access_controls_enabled defaults to false
    .with_sns_dedicated_subnets(machine.get_subnet_ids())
    // NOTE: with_sns_wasm_access_controls(true) is intentionally NOT called
    .build();
let sns_wasm_canister_id = install_sns_wasm(&machine, &nns_init_payload);

let malicious_wasm = SnsWasm {
    wasm: attacker_controlled_bytes(),
    canister_type: SnsCanisterType::Governance.into(),
    ..Default::default()
};
let hash = malicious_wasm.sha256_hash();

// Step 1: unprivileged principal adds malicious WASM — succeeds
let resp = add_wasm(&machine, sns_wasm_canister_id, malicious_wasm, &hash);
assert!(matches!(resp.result, Some(add_wasm_response::Result::Hash(_))));

// Step 2: wire malicious hash into upgrade path — succeeds
let resp2: InsertUpgradePathEntriesResponse = update_with_sender(
    &machine, sns_wasm_canister_id, "insert_upgrade_path_entries",
    InsertUpgradePathEntriesRequest { upgrade_path: vec![poisoned_entry(hash)], .. },
    PrincipalId::new_user_test_id(1337),  // unprivileged
).unwrap();
// assert upgrade path is now poisoned; next deploy_new_sns installs attacker WASM
```

### Citations

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

**File:** rs/nns/sns-wasm/canister/canister.rs (L302-310)
```rust
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

**File:** rs/nns/test_utils/src/common.rs (L88-89)
```rust
            genesis_token: GenesisTokenCanisterInitPayloadBuilder::new(),
            sns_wasms: SnsWasmCanisterInitPayloadBuilder::new(),
```

**File:** rs/nns/sns-wasm/src/pb/mod.rs (L171-202)
```rust
impl<M: StableMemory + Clone + Default> From<StableCanisterState> for SnsWasmCanister<M> {
    fn from(stable_canister_state: StableCanisterState) -> Self {
        let StableCanisterState {
            wasm_indexes,
            upgrade_path,
            sns_subnet_ids,
            deployed_sns_list,
            access_controls_enabled,
            allowed_principals,
            nns_proposal_to_deployed_sns,
        } = stable_canister_state;

        let wasm_indexes = wasm_indexes
            .into_iter()
            .map(|index| (vec_to_hash(index.hash.clone()).unwrap(), index))
            .collect();
        let stable_upgrade_path = upgrade_path.unwrap_or_default();
        let upgrade_path = UpgradePath::from(stable_upgrade_path);
        let sns_subnet_ids = sns_subnet_ids.into_iter().map(|id| id.into()).collect();
        let stable_memory = SnsWasmStableMemory::<M>::default();

        SnsWasmCanister {
            wasm_indexes,
            sns_subnet_ids,
            stable_memory,
            deployed_sns_list,
            upgrade_path,
            access_controls_enabled,
            allowed_principals,
            nns_proposal_to_deployed_sns,
        }
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
