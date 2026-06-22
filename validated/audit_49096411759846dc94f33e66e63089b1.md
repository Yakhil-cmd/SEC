### Title
Missing Empty-Controller Validation in `set_dapp_controllers` Allows Permanent Black-Holing of SNS Dapp Canisters - (File: rs/sns/root/src/lib.rs)

### Summary

`SnsRootCanister::set_dapp_controllers` accepts a `SetDappControllersRequest` whose `controller_principal_ids` field can be an empty vector. No validation rejects an empty list before the management canister `update_settings` call is issued. An authorized caller (SNS governance or swap canister) that passes an empty `controller_principal_ids` will set every targeted dapp canister's controller list to `[]`, permanently removing all controllers and black-holing those canisters — an irrevocable loss of control analogous to calling `setOwner(address(0))` in the referenced Staking.sol report.

### Finding Description

`set_dapp_controllers` in `rs/sns/root/src/lib.rs` is the SNS Root canister's privileged endpoint for reassigning the controllers of registered dapp canisters. It is callable by the SNS governance canister (via a passed governance proposal) or the SNS swap canister (during finalization of a token swap).

The function performs an authorization check and a pre-flight ownership check, but it never validates that `request.controller_principal_ids` is non-empty before issuing `update_settings` calls to the IC management canister:

```rust
// rs/sns/root/src/lib.rs  ~line 839
let request = UpdateSettings {
    canister_id: *dapp_canister_id,
    settings: CanisterSettings {
        controllers: Some(request.controller_principal_ids.clone()),  // ← can be []
        ..Default::default()
    },
    sender_canister_version: management_canister_client.canister_version(),
};
let update_result: Result<(), _> =
    management_canister_client.update_settings(request).await;
```

The IC execution environment (`rs/execution_environment/src/canister_manager.rs`) accepts an empty controller list — it simply clears all controllers:

```rust
if let Some(controllers) = settings.controllers() {
    canister.system_state.controllers.clear();
    for principal in controllers {
        canister.system_state.controllers.insert(principal);
    }
}
```

After the call succeeds, `still_controlled_by_this_canister` evaluates to `false` (SNS Root is not in the empty list), so the dapp is also removed from `dapp_canister_ids`, making the state change permanent and unrecoverable from within the SNS.

### Impact Explanation

Every dapp canister targeted by the call becomes permanently uncontrollable: no canister can upgrade, stop, delete, or change settings on it. The SNS itself loses the ability to manage those dapps. Because the IC management canister enforces the empty controller list at the protocol level, there is no recovery path short of an NNS emergency intervention (which is not guaranteed). This is a direct analog to the irrevocable loss of contract ownership described in the Staking.sol report.

### Likelihood Explanation

The SNS governance canister is an authorized caller. Any SNS neuron holder with sufficient voting power can submit a `DeregisterDappCanisters`-style or custom governance proposal that ultimately invokes `set_dapp_controllers`. A mistaken or malformed proposal payload with an empty `controller_principal_ids` — whether due to a developer error, a front-end encoding bug, or a malicious proposal — would trigger the vulnerability. The swap canister also calls this function during finalization; an edge-case bug in swap finalization logic that produces an empty list would have the same effect. No threshold-majority corruption is required; a single authorized message with a zero-length list suffices.

### Recommendation

Add an explicit guard at the top of `set_dapp_controllers` (and/or inside the per-canister loop) that rejects an empty `controller_principal_ids`:

```rust
if request.controller_principal_ids.is_empty() {
    panic!("controller_principal_ids must not be empty: \
            setting an empty controller list would black-hole dapp canisters");
}
```

Additionally, validate that none of the supplied principals is the anonymous principal (`PrincipalId::new_anonymous()`), mirroring the zero-address check recommended for `setOwner` in the Staking.sol report.

### Proof of Concept

1. An SNS is deployed with one registered dapp canister (`dapp_id`).
2. The SNS governance canister (or swap canister) calls `set_dapp_controllers` with:
   ```
   SetDappControllersRequest {
       canister_ids: Some(CanisterIds { canister_ids: vec![dapp_id] }),
       controller_principal_ids: vec![],   // ← empty
   }
   ```
3. `set_dapp_controllers` passes the authorization check (caller is governance/swap).
4. The pre-flight `canister_status` check passes (SNS Root is still a controller at this point).
5. `update_settings` is called with `controllers: Some([])`.
6. The IC management canister clears all controllers of `dapp_id`.
7. `still_controlled_by_this_canister` is `false`; `dapp_id` is removed from `dapp_canister_ids`.
8. `dapp_id` is now permanently uncontrollable — no canister on the IC can manage it.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** rs/sns/root/src/lib.rs (L762-774)
```rust
    pub async fn set_dapp_controllers<'a>(
        self_ref: &'static LocalKey<RefCell<Self>>,
        management_canister_client: &'a impl ManagementCanisterClient,
        own_canister_id: PrincipalId,
        caller: PrincipalId,
        request: &'a SetDappControllersRequest,
    ) -> SetDappControllersResponse {
        let is_authorized = self_ref.with(|self_ref| {
            caller == self_ref.borrow().swap_canister_id()
                || caller == self_ref.borrow().governance_canister_id()
        });
        // TODO(NNS1-1993): Remove this assertion and return an error type instead.
        assert!(is_authorized, "Caller ({caller}) is not authorized.");
```

**File:** rs/sns/root/src/lib.rs (L828-850)
```rust
        let still_controlled_by_this_canister =
            request.controller_principal_ids.contains(&own_canister_id);

        // Set controller(s) of dapp canisters.
        //
        // From now on, we should avoid panicking, because we'll be making
        // changes to external state, and we want to stay abreast of those
        // changes by not rolling back due to panic.
        let mut failed_updates = vec![];
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

**File:** rs/execution_environment/src/canister_manager.rs (L627-632)
```rust
        if let Some(controllers) = settings.controllers() {
            canister.system_state.controllers.clear();
            for principal in controllers {
                canister.system_state.controllers.insert(principal);
            }
        }
```

**File:** rs/sns/root/canister/canister.rs (L383-395)
```rust
#[candid_method(update)]
#[update]
async fn set_dapp_controllers(request: SetDappControllersRequest) -> SetDappControllersResponse {
    log!(INFO, "set_dapp_controllers");
    SnsRootCanister::set_dapp_controllers(
        &STATE,
        &ManagementCanisterClientImpl::<CanisterRuntime>::new(None),
        PrincipalId(ic_cdk::api::id()),
        PrincipalId(ic_cdk::api::caller()),
        &request,
    )
    .await
}
```
