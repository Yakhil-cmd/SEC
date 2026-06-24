### Title
Cycles Permanently Burned on Failed SNS Deployment Cleanup Due to Missing Pre-Delete Cycle Recovery - (`File: rs/nns/sns-wasm/canister/canister.rs`)

---

### Summary

The `CanisterApiImpl::delete_canister` function in the SNS-WASM canister explicitly acknowledges — via a `TODO(NNS1-1524)` comment — that it does not collect cycles from a canister before deleting it. Because the IC protocol permanently discards all remaining cycles when a canister is deleted, any cycles previously deposited into partially-deployed SNS canisters are irreversibly burned during the cleanup path of a failed SNS deployment.

---

### Finding Description

When `deploy_new_sns` is triggered (by NNS Governance), the SNS-WASM canister:

1. Creates up to 5 SNS canisters (`root`, `governance`, `ledger`, `swap`, `index`), each funded with `INITIAL_CANISTER_CREATION_CYCLES` (3 × 10¹² cycles each) from its own balance.
2. Installs WASMs, sets controllers, and funds canisters further.
3. On failure at any step after canister creation, calls `delete_canister` on each partially-created canister to clean up.

The `delete_canister` implementation in `CanisterApiImpl` is:

```rust
async fn delete_canister(&self, canister: CanisterId) -> Result<(), String> {
    // Try to stop the canister first
    self.stop_canister(canister).await?;

    // TODO(NNS1-1524) We need to collect the cycles from the canister before we delete it
    let response: CallResult<()> = ic_cdk::call(
        CanisterId::ic_00().get().0,
        "delete_canister",
        (CanisterIdRecord::from(canister),),
    )
    .await;
    ...
}
``` [1](#0-0) 

The IC execution environment explicitly states and implements that cycles are discarded on deletion:

```rust
// When a canister is deleted:
// - its state is permanently deleted, and
// - its cycles are discarded.
``` [2](#0-1) 

This is confirmed by the integration test comment:

```rust
// 15_000_000_000_000 cycles are burned creating the canisters before the failure
let initial_canister_creation_cycles = 3 * ONE_TRILLION as u128;
assert_eq!(
    machine.cycle_balance(SNS_WASM_CANISTER_ID),
    EXPECTED_SNS_CREATION_FEE
        - SNS_CANISTER_COUNT_AT_INSTALL as u128 * initial_canister_creation_cycles,
);
``` [3](#0-2) 

The cleanup path is triggered from `do_deploy_new_sns` via `try_cleanup_reversible_deploy_error`, which calls `delete_canister` on each canister in `canisters_to_delete`: [4](#0-3) 

The `fund_canisters` function, called after canister creation, sends cycles from the SNS-WASM canister's own balance to each SNS canister: [5](#0-4) 

---

### Impact Explanation

**Vulnerability class: cycles/resource accounting bug.**

Each failed SNS deployment that reaches the canister-creation step causes permanent, irrecoverable loss of up to `SNS_CANISTER_COUNT_AT_INSTALL × INITIAL_CANISTER_CREATION_CYCLES` = 5 × 3 × 10¹² = **15 trillion cycles** from the SNS-WASM canister's balance. These cycles are burned by the IC subnet when the canisters are deleted without prior cycle recovery. The SNS-WASM canister is a system canister funded by the NNS treasury; repeated or adversarially-induced deployment failures drain this balance, eventually preventing any future SNS deployments until the canister is manually re-funded via NNS proposal.

---

### Likelihood Explanation

Any NNS governance participant can submit a `CreateServiceNervousSystem` proposal. If the proposal passes and the SNS deployment fails mid-way (e.g., due to a malformed `SnsInitPayload` that passes pre-execution validation but fails at WASM install time, a transient subnet error, or a deliberately crafted bad WASM added via a prior `add_wasm` proposal), the cleanup path fires and burns cycles. The `test_deploy_cleanup_on_wasm_install_failure` integration test demonstrates this is a reachable, tested code path. The TODO comment confirms the developers are aware the cycles are not recovered. Likelihood is **medium**: requires a governance proposal to pass, but the cycle loss is a guaranteed consequence of any mid-deployment failure.

---

### Recommendation

Before calling `delete_canister` on the management canister, the SNS-WASM canister should first call `deposit_cycles` (or equivalent) to transfer the target canister's remaining cycles back to the SNS-WASM canister's own balance. Specifically, resolve `TODO(NNS1-1524)` in `CanisterApiImpl::delete_canister`:

```rust
async fn delete_canister(&self, canister: CanisterId) -> Result<(), String> {
    self.stop_canister(canister).await?;

    // Recover cycles before deletion
    let _: CallResult<()> = ic_cdk::api::call::call_with_payment(
        CanisterId::ic_00().get().0,
        "deposit_cycles",
        (CanisterIdRecord::from(self.local_canister_id()),),
        0, // cycles come from the target canister's balance via uninstall+deposit pattern
    ).await;
    // ... then delete
}
```

The correct pattern is to use `ic00::UninstallCode` followed by a `deposit_cycles` call, or to use the `stop_canister` → `deposit_cycles` → `delete_canister` sequence so that the target canister's balance is transferred back before deletion.

---

### Proof of Concept

1. Submit a `CreateServiceNervousSystem` NNS proposal with a valid payload that passes `validate_post_execution()` but whose governance WASM init payload is incompatible (e.g., use the universal canister WASM for the governance slot, as done in `test_deploy_cleanup_on_wasm_install_failure`).
2. The proposal passes; `deploy_new_sns` is called on SNS-WASM.
3. SNS-WASM creates 5 canisters, each receiving 3T cycles from its balance (15T total).
4. WASM installation on the governance canister fails.
5. `try_cleanup_reversible_deploy_error` calls `delete_canister` on all 5 canisters.
6. `CanisterApiImpl::delete_canister` stops and deletes each canister **without recovering cycles**.
7. The IC subnet burns all 15T cycles.
8. SNS-WASM's balance is reduced by 15T cycles with no recovery.

This is directly confirmed by the existing integration test: [6](#0-5)

### Citations

**File:** rs/nns/sns-wasm/canister/canister.rs (L94-110)
```rust
    /// See CanisterApi::delete_canister
    async fn delete_canister(&self, canister: CanisterId) -> Result<(), String> {
        // Try to stop the canister first
        self.stop_canister(canister).await?;

        // TODO(NNS1-1524) We need to collect the cycles from the canister before we delete it
        let response: CallResult<()> = ic_cdk::call(
            CanisterId::ic_00().get().0,
            "delete_canister",
            (CanisterIdRecord::from(canister),),
        )
        .await;

        response.map_err(handle_call_error(format!(
            "Failed to delete canister {canister}"
        )))
    }
```

**File:** rs/execution_environment/src/canister_manager.rs (L1280-1285)
```rust
        // When a canister is deleted:
        // - its state is permanently deleted, and
        // - its cycles are discarded.

        // Remove the canister from `ReplicatedState`.
        let canister_to_delete = state.remove_canister(&canister_id_to_delete).unwrap();
```

**File:** rs/nns/sns-wasm/tests/deploy_new_sns.rs (L167-248)
```rust
fn test_deploy_cleanup_on_wasm_install_failure() {
    let machine = set_up_state_machine_with_nns();

    // Add cycles to the SNS-W canister to deploy an SNS.
    machine.add_cycles(SNS_WASM_CANISTER_ID, EXPECTED_SNS_CREATION_FEE);

    sns_wasm::add_real_wasms_to_sns_wasms(&machine);
    // we add a wasm that will fail with the given payload on installation
    let bad_wasm = SnsWasm {
        wasm: Wasm::from_bytes(UNIVERSAL_CANISTER_WASM.to_vec()).bytes(),
        canister_type: SnsCanisterType::Governance.into(),
        ..SnsWasm::default()
    };
    sns_wasm::add_wasm_via_proposal(&machine, bad_wasm);

    let sns_init_payload = SnsInitPayload {
        dapp_canisters: None,
        ..SnsInitPayload::with_valid_values_for_testing_post_execution()
    };

    let response = sns_wasm::deploy_new_sns(
        &machine,
        GOVERNANCE_CANISTER_ID,
        SNS_WASM_CANISTER_ID,
        sns_init_payload,
    );

    let highest_nns_created_canister_index = NODE_REWARDS_CANISTER_INDEX_IN_NNS_SUBNET;

    let root = canister_test_id(highest_nns_created_canister_index + 1);
    let governance = canister_test_id(highest_nns_created_canister_index + 2);
    let ledger = canister_test_id(highest_nns_created_canister_index + 3);
    let swap = canister_test_id(highest_nns_created_canister_index + 4);
    let index = canister_test_id(highest_nns_created_canister_index + 5);
    let error_message = response.error.clone().unwrap().message;
    let expected_error = format!(
        "Error installing Governance WASM: Failed to install WASM on canister \
        {governance}: error code 5: Error from Canister {governance}: \
        Canister called `ic0.trap` with message: 'did not find blob on stack"
    );
    assert!(
        error_message.contains(&expected_error),
        "Response error \"{error_message}\" does not contain expected error \"{expected_error}\""
    );

    assert_eq!(
        response,
        DeployNewSnsResponse {
            subnet_id: Some(machine.get_subnet_ids().first().unwrap().get()),
            canisters: Some(SnsCanisterIds {
                root: Some(root.get()),
                ledger: Some(ledger.get()),
                governance: Some(governance.get()),
                swap: Some(swap.get()),
                index: Some(index.get()),
            }),
            // Because of the invalid WASM above (i.e. universal canister) which does not understand
            // the governance init payload, this fails.
            error: Some(SnsWasmError {
                message: error_message,
            }),
            dapp_canisters_transfer_result: Some(DappCanistersTransferResult {
                restored_dapp_canisters: vec![],
                sns_controlled_dapp_canisters: vec![],
                nns_controlled_dapp_canisters: vec![],
            }),
        }
    );

    // No canisters should exist above highest_nns_created_canister_index because we deleted
    // those canisters.
    for i in 1..=5 {
        assert!(!machine.canister_exists(canister_test_id(highest_nns_created_canister_index + i)));
    }

    // 15_000_000_000_000 cycles are burned creating the canisters before the failure
    let initial_canister_creation_cycles = 3 * ONE_TRILLION as u128;
    assert_eq!(
        machine.cycle_balance(SNS_WASM_CANISTER_ID),
        EXPECTED_SNS_CREATION_FEE
            - SNS_CANISTER_COUNT_AT_INSTALL as u128 * initial_canister_creation_cycles,
    );
```

**File:** rs/nns/sns-wasm/src/sns_wasm.rs (L786-804)
```rust
            Err(DeployError::Reversible(reversible)) => {
                // Attempt to clean up after normal failures.
                Self::try_cleanup_reversible_deploy_error(
                    canister_api,
                    nns_root_canister_client,
                    reversible,
                )
                .await
            }
            Err(DeployError::PartiallyReversible(partially_reversible)) => {
                // Attempt to clean up after abnormal failures.
                Self::try_cleanup_partially_reversible_deploy_error(
                    nns_root_canister_client,
                    partially_reversible,
                )
                .await
            }
            // The rest are conversions as no additional processing is needed
            Err(e) => e.into(),
```

**File:** rs/nns/sns-wasm/src/sns_wasm.rs (L1030-1067)
```rust
    /// Accept remaining cycles in the request, subtract the cycles we've already used, and distribute
    /// the remainder among the canisters
    async fn fund_canisters(
        canister_api: &impl CanisterApi,
        canisters: &SnsCanisterIds,
    ) -> Result<(), String> {
        // Accept the remaining cycles in the request we need to fund the canisters
        let remaining_unaccepted_cycles = SNS_CREATION_FEE.saturating_sub(
            INITIAL_CANISTER_CREATION_CYCLES.saturating_mul(SNS_CANISTER_COUNT_AT_INSTALL),
        );
        // We only collect the INITIAL_CANISTER_CREATION_CYCLES for the other 5 canisters because
        // archive will be created by the ledger post deploy.  In order to split whole allocation
        // evenly between all 6 canisters, we want to account for this.
        let uncollected_allocation_for_archive = INITIAL_CANISTER_CREATION_CYCLES;
        let cycles_per_canister = (remaining_unaccepted_cycles
            .saturating_sub(uncollected_allocation_for_archive))
        .saturating_div(SNS_CANISTER_TYPE_COUNT);

        let results = futures::future::join_all(canisters.into_named_tuples().into_iter().map(
            |(label, canister_id)| async move {
                // Ledger needs 2x as many because it will spawn an archive
                let cycles_to_provide = if label == "Ledger" {
                    // Give ledger the cycles archive would have gotten were it created the same
                    // as all of the other canisters.
                    cycles_per_canister * 2 + uncollected_allocation_for_archive
                } else {
                    cycles_per_canister
                };
                canister_api
                    .send_cycles_to_canister(canister_id, cycles_to_provide)
                    .await
                    .map_err(|e| format!("Could not fund {label} canister: {e}"))
            },
        ))
        .await;

        join_errors_or_ok(results)
    }
```
