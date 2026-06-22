### Title
Dapp Canister Registration Proceeds Without Checking Canister Running Status - (File: `rs/sns/root/src/lib.rs`)

### Summary
The `register_dapp_canister` function in `SnsRootCanister` fetches `canister_status` to verify controller ownership but never inspects the canister's lifecycle state (`Running`/`Stopping`/`Stopped`). A stopped or stopping dapp canister can be successfully registered, after which SNS governance proposals that attempt to interact with it will fail silently or panic, blocking SNS operations on the newly registered dapp.

### Finding Description
In `rs/sns/root/src/lib.rs`, the private `register_dapp_canister` function (called by the public `register_dapp_canisters` endpoint) performs the following validation steps:

1. Checks registration limits and SNS-distinguished-canister exclusions.
2. Calls `management_canister_client.canister_status(canister_to_register.into())` to obtain the canister's status record.
3. Checks only `canister_status.controllers()` to verify SNS root is a controller.
4. Optionally calls `update_settings` to strip extra controllers.
5. Adds the canister to `self.dapp_canister_ids`. [1](#0-0) 

The `canister_status` response from the IC management canister includes both a `controllers` field and a `status` field (`running` / `stopping` / `stopped`). The code reads `canister_status.controllers()` but **never reads `canister_status.status()`**. There is no guard of the form:

```rust
if canister_status.status() != CanisterStatusType::Running {
    Err("Canister is not running")?;
}
```

As a result, a canister in `Stopped` or `Stopping` state passes all validation checks and is appended to `dapp_canister_ids` without error. [2](#0-1) 

The public entry point `register_dapp_canisters` in `canister.rs` enforces that only the SNS governance canister may call it, but any SNS token holder can submit a `RegisterDappCanisters` governance proposal that names a stopped canister. [3](#0-2) 

The `CanisterStatusType` enum confirms the three possible states that the code never validates against: [4](#0-3) 

The same omission exists in `register_extension` (lines 483–604), which calls `update_settings` and then `canister_status` to verify controllers but also never checks the running state of the extension canister being registered. [5](#0-4) 

### Impact Explanation
Once a stopped dapp canister is registered:

- **SNS upgrade proposals** targeting the dapp canister will fail: `change_canister` stops the canister before upgrading, but a canister already stopped will cause the stop-and-upgrade flow to behave unexpectedly.
- **`get_sns_canisters_summary`** will report the dapp as stopped, misleading token holders about the health of the SNS.
- **`set_dapp_controllers`** (called during swap finalization) iterates over all registered dapp canisters and calls `canister_status` on each; a stopped canister will return a stopped status, and any subsequent attempt to call update methods on it will be rejected by the IC with `CanisterStopped`.
- Any SNS governance proposal that calls an update method on the registered dapp canister will be permanently blocked until the canister is manually restarted — but SNS root's `change_canister` flow is the only exposed restart path, and it requires a separate governance proposal. [6](#0-5) 

### Likelihood Explanation
The attack path is reachable by any unprivileged SNS token holder:

1. Obtain a small amount of SNS governance tokens (or use an existing neuron).
2. Submit a `RegisterDappCanisters` proposal naming a canister that is currently stopped (e.g., a dapp that was stopped for maintenance, or one the attacker controls and deliberately stopped).
3. If the proposal passes (normal SNS voting), SNS root registers the stopped canister without any lifecycle check.
4. Subsequent SNS operations on that dapp are blocked.

No privileged access, no key compromise, and no subnet-majority attack is required. The governance proposal path is the standard, publicly accessible mechanism for registering dapp canisters.

### Recommendation
In `register_dapp_canister` (`rs/sns/root/src/lib.rs`), after obtaining `canister_status`, add an explicit check against the canister's lifecycle state before proceeding with registration:

```rust
let canister_status = management_canister_client
    .canister_status(canister_to_register.into())
    .await
    .map_err(|err| format!("Canister status unavailable: {err:?}"))?;

// NEW: Reject if the canister is not running.
if canister_status.status() != CanisterStatusType::Running {
    Err(format!(
        "Canister {canister_to_register} is not running (status: {:?}); \
         only running canisters may be registered as dapp canisters.",
        canister_status.status()
    ))?;
}

// Reject if we do not have control.
if !canister_status.controllers().contains(&root_canister_id) { ... }
```

Apply the same guard in `register_extension`. Expand the unit test suite to cover the stopped and stopping canister cases as unhappy-path scenarios.

### Proof of Concept
1. Deploy an SNS on a test subnet.
2. Create a dapp canister controlled by SNS root.
3. Stop the dapp canister via the management canister (or have its original controller stop it before transferring control).
4. Submit a `RegisterDappCanisters` SNS governance proposal naming the stopped canister.
5. Observe that the proposal executes successfully and the stopped canister appears in `dapp_canister_ids`.
6. Submit a subsequent SNS governance proposal to upgrade the stopped dapp canister.
7. Observe that the upgrade proposal fails or panics because the canister is stopped, blocking SNS governance operations on that dapp.

The root cause is confirmed at: [7](#0-6) 

where `canister_status` is fetched and `controllers()` is checked, but `status()` is never consulted before the canister is appended to `dapp_canister_ids`.

### Citations

**File:** rs/sns/root/src/lib.rs (L557-591)
```rust
        // Verify that the extension is now controlled only by Root and Governance.
        {
            let canister_id = CanisterId::unchecked_from_principal(canister_id);

            let canister_status = management_canister_client
                .canister_status(canister_id.into())
                .await
                .map_err(|(code, message)| {
                    let description = format!("Canister status unavailable: {message}");
                    CanisterCallError {
                        code: Some(code),
                        description,
                    }
                })?;

            let controllers = canister_status
                .controllers()
                .iter()
                .copied()
                .collect::<BTreeSet<_>>();

            if controllers != expected_controllers {
                let controllers = controllers
                    .iter()
                    .map(|p| p.to_string())
                    .collect::<Vec<_>>()
                    .join(", ");

                return reject(&format!(
                    "Extension canister must be controlled by Root ({root_canister_id}) and Governance ({governance_canister_id}) \
                     of this SNS. However, despite the update_settings call seemingly \
                     succeeding, extension canister ({canister_id}) is still controlled by {controllers}.",
                ));
            }
        }
```

**File:** rs/sns/root/src/lib.rs (L702-745)
```rust
        // Make sure we are a controller by querying the management canister.
        let canister_status = management_canister_client
            .canister_status(canister_to_register.into())
            .await
            .map_err(|err| format!("Canister status unavailable: {err:?}"))?;

        // Reject if we do not have control.
        if !canister_status.controllers().contains(&root_canister_id) {
            Err("Canister is not controlled by this SNS root canister")?;
        }

        // If testflight is not active, we want to make sure root is the
        // only controller.
        let root_is_only_controller = canister_status.controllers() == vec![root_canister_id];
        if !testflight && !root_is_only_controller {
            // Remove all controllers except for root.
            management_canister_client
                .update_settings(UpdateSettings {
                    canister_id: canister_to_register.into(),
                    settings: CanisterSettings {
                        controllers: Some(vec![root_canister_id]),
                        ..Default::default()
                    },
                    sender_canister_version: management_canister_client.canister_version(),
                })
                .await
                .map_err(|err| format!("Controller change failed: {err:?}"))?;

            // Verify that we are the only controller.
            // This is a sanity check, and should never fail.
            let canister_status = management_canister_client
                .canister_status(canister_to_register.into())
                .await
                .map_err(|err| format!("Canister status unavailable: {err:?}"))?;
            if canister_status.controllers() != vec![root_canister_id] {
                Err("Controller change failed")?;
            }
        }
        // Add canister_to_register to self.dapp_canister_ids.
        self_ref.with_borrow_mut(|state| {
            let canister_to_register = PrincipalId::from(canister_to_register);
            state.dapp_canister_ids.push(canister_to_register);
        });
        Ok(())
```

**File:** rs/sns/root/canister/canister.rs (L351-365)
```rust
#[candid_method(update)]
#[update]
async fn register_dapp_canisters(
    request: RegisterDappCanistersRequest,
) -> RegisterDappCanistersResponse {
    log!(INFO, "register_dapp_canisters");
    assert_eq_governance_canister_id(PrincipalId(ic_cdk::api::caller()));
    SnsRootCanister::register_dapp_canisters(
        &STATE,
        &ManagementCanisterClientImpl::<CanisterRuntime>::new(None),
        PrincipalId(ic_cdk::api::id()),
        request,
    )
    .await
}
```

**File:** rs/types/management_canister_types/src/lib.rs (L1776-1784)
```rust
#[derive(Clone, Eq, PartialEq, Hash, Debug, CandidType, Deserialize, Serialize)]
pub enum CanisterStatusType {
    #[serde(rename = "running")]
    Running,
    #[serde(rename = "stopping")]
    Stopping,
    #[serde(rename = "stopped")]
    Stopped,
}
```

**File:** rs/execution_environment/src/execution/common.rs (L331-342)
```rust
pub(crate) fn validate_canister(canister: &CanisterState) -> Result<(), UserError> {
    if CanisterStatusType::Running != canister.status() {
        let canister_id = canister.canister_id();
        let err_code = match canister.status() {
            CanisterStatusType::Running => unreachable!(),
            CanisterStatusType::Stopping => ErrorCode::CanisterStopping,
            CanisterStatusType::Stopped => ErrorCode::CanisterStopped,
        };
        let err_msg = format!("Canister {canister_id} is not running");
        return Err(UserError::new(err_code, err_msg));
    }
    Ok(())
```
