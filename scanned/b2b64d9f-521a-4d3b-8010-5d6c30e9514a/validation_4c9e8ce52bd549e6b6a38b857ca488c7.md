### Title
Missing SNS Framework Canister Exclusion in `set_dapp_controllers` Allows Governance Takeover - (`rs/sns/root/src/lib.rs`)

### Summary

`SnsRootCanister::set_dapp_controllers` accepts a caller-supplied list of canister IDs and calls `update_settings` on each of them via the management canister. When `request.canister_ids` is `Some(...)`, the function uses the list verbatim with **no validation** that the provided IDs are registered dapp canisters and **no exclusion** of SNS framework canisters (governance, ledger, index, swap, root). Because root always controls governance by design, the pre-flight `canister_status` check passes for `governance_canister_id`, and `update_settings` is then called on the governance canister with an attacker-controlled principal as the new controller.

### Finding Description

In `rs/sns/root/src/lib.rs`, the `set_dapp_controllers` function performs two checks before operating:

1. **Authorization** (lines 769–774): caller must be swap or governance canister.
2. **Pre-flight control check** (lines 796–826): root must currently control each canister in the list.

When `canister_ids` is `Some(...)`, the code at line 778 directly clones the caller-supplied list:

```rust
Some(canister_ids) => canister_ids.canister_ids.clone(),
``` [1](#0-0) 

There is no step that cross-references this list against `self_ref.borrow().dapp_canister_ids` (the registered dapp set), and no step that rejects IDs matching `governance_canister_id`, `ledger_canister_id`, `index_canister_id`, `swap_canister_id`, or `own_canister_id` (root itself).

The pre-flight loop (lines 796–826) only asserts that root is a controller of each supplied canister — a condition that is always true for the governance canister: [2](#0-1) 

After the pre-flight passes, `update_settings` is called unconditionally for every ID in the list: [3](#0-2) 

### Impact Explanation

If the swap canister (an authorized caller) sends `set_dapp_controllers` with `canister_ids=Some([governance_canister_id])` and `controller_principal_ids=[attacker]`, root will invoke `management_canister.update_settings` on the SNS governance canister, replacing its controllers with the attacker's principal. The attacker then has full control of governance: they can mint unlimited SNS tokens, drain the treasury, dissolve neurons, and modify all SNS parameters.

### Likelihood Explanation

The swap canister is an authorized caller and calls `set_dapp_controllers` during swap finalization (Committed or Aborted lifecycle). The root canister provides no defense-in-depth validation of the supplied canister IDs. Any bug in the swap canister that allows an attacker to influence the `canister_ids` field — or a compromised swap canister — directly leads to full SNS governance takeover. The missing invariant is a single missing membership check against `dapp_canister_ids` and a missing exclusion list for SNS framework canisters.

### Recommendation

In `set_dapp_controllers`, when `canister_ids` is `Some(...)`, validate each supplied ID against the registered dapp set and explicitly reject any ID that matches an SNS framework canister:

```rust
Some(canister_ids) => {
    let sns_canister_ids = self_ref.with(|s| {
        let s = s.borrow();
        [
            s.governance_canister_id(),
            s.ledger_canister_id(),
            s.index_canister_id(),
            s.swap_canister_id(),
            own_canister_id,
        ]
    });
    let registered_dapps: HashSet<_> =
        self_ref.with(|s| s.borrow().dapp_canister_ids.iter().cloned().collect());
    for id in &canister_ids.canister_ids {
        assert!(
            !sns_canister_ids.contains(id),
            "Refusing to change controllers of SNS framework canister {id}"
        );
        assert!(
            registered_dapps.contains(id),
            "Canister {id} is not a registered dapp canister"
        );
    }
    canister_ids.canister_ids.clone()
}
``` [4](#0-3) 

### Proof of Concept

State-machine test outline:

1. Initialize an SNS with governance, ledger, index, swap, root canisters; register zero dapp canisters.
2. From the swap canister principal, call `root.set_dapp_controllers` with:
   - `canister_ids = Some(CanisterIds { canister_ids: [governance_canister_id] })`
   - `controller_principal_ids = [attacker_principal]`
3. Assert the call returns `SetDappControllersResponse { failed_updates: [] }`.
4. Call `management_canister.canister_status(governance_canister_id)` and assert `controllers == [attacker_principal]`.
5. From `attacker_principal`, call `governance.manage_neuron` or `governance.set_mode` to confirm full control.

The existing test `test_set_dapp_controllers_some_canisters` (lines 2134–2203) demonstrates that the swap canister can successfully call `set_dapp_controllers` with an explicit `canister_ids` list and that `update_settings` is invoked — it simply uses dapp IDs rather than framework IDs, confirming the code path is live and the only missing guard is the membership/exclusion check. [5](#0-4)

### Citations

**File:** rs/sns/root/src/lib.rs (L776-790)
```rust
        // Grab a snapshot of canisters to operate on.
        let dapp_canister_ids = match &request.canister_ids {
            Some(canister_ids) => canister_ids.canister_ids.clone(),
            // If no canister list is specified, we take all the canisters controlled by root.
            None => {
                let is_authorized_to_set_all_controllers =
                    self_ref.with(|self_ref| caller == self_ref.borrow().swap_canister_id());
                if is_authorized_to_set_all_controllers {
                    self_ref.with(|self_ref| self_ref.borrow().dapp_canister_ids.clone())
                } else {
                    // TODO(NNS1-1993): Remove this panic and return an error type instead.
                    panic!("Only the swap canister is authorized to set all dapp controllers")
                }
            }
        };
```

**File:** rs/sns/root/src/lib.rs (L815-825)
```rust
            let is_controllee = canister_status.controllers().contains(&own_canister_id);

            // TODO(NNS1-1993): Remove this assertion and return an error type instead.
            assert!(
                is_controllee,
                "Operation aborted due to an error; no changes have been made: \
                 Unable to determine whether this canister (SNS root) is the controller \
                 of a registered dapp canister ({dapp_canister_id}). This may be due to \
                 the canister having been deleted, which may be due to it running out \
                 of cycles."
            );
```

**File:** rs/sns/root/src/lib.rs (L837-850)
```rust
        for dapp_canister_id in &dapp_canister_ids {
            // Prepare to call management canister.
            let request = UpdateSettings {
                canister_id: *dapp_canister_id,
                settings: CanisterSettings {
                    controllers: Some(request.controller_principal_ids.clone()),
                    ..Default::default()
                },
                sender_canister_version: management_canister_client.canister_version(),
            };

            // Perform the call.
            let update_result: Result<(), _> =
                management_canister_client.update_settings(request).await;
```

**File:** rs/sns/root/src/lib.rs (L2134-2203)
```rust
    async fn test_set_dapp_controllers_some_canisters() {
        // Step 1: Prepare the world.
        thread_local! {
            static STATE: RefCell<SnsRootCanister> = RefCell::new(SnsRootCanister {
                governance_canister_id: Some(PrincipalId::new_user_test_id(1)),
                ledger_canister_id: Some(PrincipalId::new_user_test_id(2)),
                swap_canister_id: Some(PrincipalId::new_user_test_id(99)),
                dapp_canister_ids: vec![PrincipalId::new_user_test_id(3), PrincipalId::new_user_test_id(4), PrincipalId::new_user_test_id(5), PrincipalId::new_user_test_id(6)],
                archive_canister_ids: vec![],
                ..Default::default()
            });
        }
        let sns_root_canister_id = PrincipalId::new_user_test_id(4);
        let new_controller_principal_id = PrincipalId::new_user_test_id(5);

        // Step 1.1: Prepare helpers.
        let management_canister_client = MockManagementCanisterClient::new(vec![
            MockManagementCanisterClientReply::CanisterStatus(Ok(
                CanisterStatusResultFromManagementCanister::dummy_with_controllers(vec![
                    sns_root_canister_id,
                ]),
            )),
            MockManagementCanisterClientReply::CanisterStatus(Ok(
                CanisterStatusResultFromManagementCanister::dummy_with_controllers(vec![
                    sns_root_canister_id,
                ]),
            )),
            MockManagementCanisterClientReply::CanisterStatus(Ok(
                CanisterStatusResultFromManagementCanister::dummy_with_controllers(vec![
                    sns_root_canister_id,
                ]),
            )),
            MockManagementCanisterClientReply::UpdateSettings(Ok(())),
            MockManagementCanisterClientReply::UpdateSettings(Ok(())),
            MockManagementCanisterClientReply::UpdateSettings(Ok(())),
        ]);

        // Step 2: Run code under test.
        let response = SnsRootCanister::set_dapp_controllers(
            &STATE,
            &management_canister_client,
            sns_root_canister_id,
            STATE.with(|state| state.borrow().swap_canister_id.unwrap()),
            &SetDappControllersRequest {
                // Change controller to all dapps controlled by the root canister.
                canister_ids: Some(CanisterIds {
                    canister_ids: vec![
                        PrincipalId::new_user_test_id(4),
                        PrincipalId::new_user_test_id(5),
                        PrincipalId::new_user_test_id(6),
                    ],
                }),
                controller_principal_ids: vec![new_controller_principal_id],
            },
        )
        .await;

        // Step 3: Inspect results.
        assert_eq!(
            response,
            SetDappControllersResponse {
                failed_updates: vec![]
            }
        );
        let state = &STATE.with(|state| state.borrow().clone());
        assert_eq!(
            state.dapp_canister_ids,
            vec![PrincipalId::new_user_test_id(3)],
            "{state:#?}",
        );
```
