### Title
VIP Caller Bypass Broken in `try_borrow_slot`: Non-VIP Slot Exhaustion Blocks NNS Canister Management Operations — (`rs/nervous_system/clients/src/management_canister_client.rs`)

---

### Summary

The `try_borrow_slot` function in `LimitedOutstandingCallsManagementCanisterClient` contains a logic error: the `available_slot_count == 0` guard fires unconditionally for **all** callers, including VIP callers (`is_caller_vip=true`), before the VIP-specific `used_slot_count = 0` path can take effect. An unprivileged user can exhaust all 167 slots via the public `canister_status` endpoint on the NNS root canister, causing VIP callers (NNS canisters such as governance) to receive `SysTransient` errors on subsequent management canister operations routed through root.

---

### Finding Description

In `try_borrow_slot`: [1](#0-0) 

```rust
fn try_borrow_slot(&self) -> Result<SlotLoan, (i32, String)> {
    let used_slot_count = if self.is_caller_vip { 0 } else { 1 };

    self.available_slot_count
        .with_borrow_mut(|available_slot_count| {
            if *available_slot_count == 0 {          // ← fires for ALL callers
                let code = RejectCode::SysTransient as i32;
                let message = "Unavailable. Maybe, try again later?".to_string();
                return Err((code, message));
            }
            *available_slot_count = available_slot_count.saturating_sub(used_slot_count);
            Ok(())
        })?;
    ...
}
```

The intent is that VIP callers consume zero slots (`used_slot_count = 0`), so they should never be rate-limited. However, the `== 0` guard is evaluated **before** the subtraction, and it does not check `is_caller_vip`. When `available_slot_count` reaches zero (all 167 slots consumed by non-VIP callers), the guard returns `Err` for every caller regardless of VIP status.

The slot pool is initialized to 167 in the NNS root canister: [2](#0-1) 

The `canister_status` endpoint on the NNS root canister is explicitly public (no access control), and `new_management_canister_client()` is called per-request with `is_caller_vip` derived from the caller: [3](#0-2) [4](#0-3) 

---

### Impact Explanation

An unprivileged attacker sends 167 concurrent update calls to the public `canister_status` endpoint on the NNS root canister. Each call suspends at the `await management_canister.canister_status()` point (IC async model: canister processes next message while awaiting inter-canister response), consuming one slot. Once `available_slot_count == 0`, any subsequent call — including from NNS governance or other NNS canisters — hits the guard and receives `SysTransient`. This blocks:

- `canister_status` (public endpoint, but also used internally by root during `change_nns_canister`)
- `stop_canister`, `update_settings`, `delete_canister`, `take_canister_snapshot`, `load_canister_snapshot` — all route through `try_borrow_slot` [5](#0-4) 

The DoS is sustained as long as the attacker continuously replenishes calls. Management canister calls complete in seconds, so the attacker must maintain a steady stream of ~167 concurrent calls. This is feasible via repeated ingress submissions.

---

### Likelihood Explanation

- The `canister_status` endpoint has no access control and is documented as intentionally public.
- The IC message queue limit is 500; 167 concurrent in-flight calls is well within reach.
- The bug is deterministic and reproducible in a state-machine test.
- No privileged access, key material, or consensus corruption is required.

---

### Recommendation

Add a VIP bypass to the `== 0` guard:

```rust
if !self.is_caller_vip && *available_slot_count == 0 {
    let code = RejectCode::SysTransient as i32;
    let message = "Unavailable. Maybe, try again later?".to_string();
    return Err((code, message));
}
```

This ensures VIP callers always proceed regardless of slot exhaustion, which matches the documented intent ("VIP = is an NNS canister"). [6](#0-5) 

---

### Proof of Concept

A state-machine test would:

1. Install the NNS root canister with `AVAILABLE_MANAGEMENT_CANISTER_CALL_SLOT_COUNT = 167`.
2. Submit 167 concurrent non-VIP `canister_status` update calls (e.g., from anonymous principal) targeting a slow-responding canister to hold slots open.
3. Assert `available_slot_count == 0`.
4. Submit a `canister_status` call from an NNS canister principal (VIP, `is_caller_vip=true`).
5. Assert the response is `Err((SysTransient, "Unavailable..."))` — demonstrating VIP is blocked despite `used_slot_count=0`.

The existing test infrastructure at `rs/nns/handlers/root/impl/tests/test.rs` already uses `state_machine_test_on_nns_subnet` and `update_with_sender`, making this directly reproducible. [7](#0-6)

### Citations

**File:** rs/nervous_system/clients/src/management_canister_client.rs (L264-287)
```rust
    fn try_borrow_slot(&self) -> Result<SlotLoan, (i32, String)> {
        let used_slot_count = if self.is_caller_vip { 0 } else { 1 };

        self.available_slot_count
            .with_borrow_mut(|available_slot_count| {
                if *available_slot_count == 0 {
                    // This is somewhat of a lie, but is the best fit.
                    let code = RejectCode::SysTransient as i32;

                    let message = "Unavailable. Maybe, try again later?".to_string();

                    return Err((code, message));
                }

                *available_slot_count = available_slot_count.saturating_sub(used_slot_count);
                Ok(())
            })?;

        let available_slot_count = self.available_slot_count;
        Ok(SlotLoan {
            available_slot_count,
            used_slot_count,
        })
    }
```

**File:** rs/nervous_system/clients/src/management_canister_client.rs (L295-357)
```rust
    async fn canister_status(
        &self,
        canister_id_record: CanisterIdRecord,
    ) -> Result<CanisterStatusResultFromManagementCanister, (i32, String)> {
        let _loan = self.try_borrow_slot()?;
        self.inner.canister_status(canister_id_record).await
    }

    async fn update_settings(&self, settings: UpdateSettings) -> Result<(), (i32, String)> {
        let _loan = self.try_borrow_slot()?;
        self.inner.update_settings(settings).await
    }

    async fn canister_metadata(
        &self,
        canister_id: PrincipalId,
        name: String,
    ) -> Result<Vec<u8>, (i32, String)> {
        let _loan = self.try_borrow_slot()?;
        self.inner.canister_metadata(canister_id, name).await
    }

    fn canister_version(&self) -> Option<u64> {
        // This does not actually call the management canister. This implies a few things:
        //
        //   1. No need to call try_borrow_slot, as is done elsewhere.
        //   2. It was a mistake for this method to be included in this trait.
        //   3. No need for this method to be async.
        self.inner.canister_version()
    }

    async fn stop_canister(
        &self,
        canister_id_record: CanisterIdRecord,
    ) -> Result<(), (i32, String)> {
        let _loan = self.try_borrow_slot()?;
        self.inner.stop_canister(canister_id_record).await
    }

    async fn delete_canister(
        &self,
        canister_id_record: CanisterIdRecord,
    ) -> Result<(), (i32, String)> {
        let _loan = self.try_borrow_slot()?;
        self.inner.delete_canister(canister_id_record).await
    }

    async fn take_canister_snapshot(
        &self,
        args: TakeCanisterSnapshotArgs,
    ) -> Result<CanisterSnapshotResponse, (i32, String)> {
        let _loan = self.try_borrow_slot()?;
        self.inner.take_canister_snapshot(args).await
    }

    async fn load_canister_snapshot(
        &self,
        args: LoadCanisterSnapshotArgs,
    ) -> Result<(), (i32, String)> {
        let _loan = self.try_borrow_slot()?;
        self.inner.load_canister_snapshot(args).await
    }
}
```

**File:** rs/nns/handlers/root/impl/canister/canister.rs (L47-51)
```rust
thread_local! {
    // How this value was chosen: queues become full at 500. This is 1/3 of that, which seems to be
    // a reasonable balance.
    static AVAILABLE_MANAGEMENT_CANISTER_CALL_SLOT_COUNT: RefCell<u64> = const { RefCell::new(167) };
}
```

**File:** rs/nns/handlers/root/impl/canister/canister.rs (L53-67)
```rust
fn new_management_canister_client() -> impl ManagementCanisterClient {
    let client =
        ManagementCanisterClientImpl::<CdkRuntime>::new(Some(&PROXIED_CANISTER_CALLS_TRACKER));

    // Here, VIP = is an NNS canister
    let is_caller_vip = CanisterId::try_from(caller())
        .map(|caller| ALL_NNS_CANISTER_IDS.contains(&&caller))
        .unwrap_or(false);

    LimitedOutstandingCallsManagementCanisterClient::new(
        client,
        &AVAILABLE_MANAGEMENT_CANISTER_CALL_SLOT_COUNT,
        is_caller_vip,
    )
}
```

**File:** rs/nns/handlers/root/impl/canister/canister.rs (L88-98)
```rust
#[update]
async fn canister_status(canister_id_record: CanisterIdRecord) -> CanisterStatusResult {
    let client = new_management_canister_client();

    let canister_status_response = client
        .canister_status(canister_id_record)
        .await
        .map(CanisterStatusResult::from);

    canister_status_response.unwrap()
}
```

**File:** rs/nns/handlers/root/impl/tests/test.rs (L1-51)
```rust
use assert_matches::assert_matches;
use candid::Encode;
use dfn_candid::candid;
use ic_base_types::{CanisterId, PrincipalId};
use ic_management_canister_types_private::CanisterInstallMode::Upgrade;
use ic_nervous_system_clients::{
    canister_id_record::CanisterIdRecord, canister_status::CanisterStatusResult,
};
use ic_nervous_system_proxied_canister_calls_tracker::ProxiedCanisterCallsTracker;
use ic_nervous_system_root::change_canister::ChangeCanisterRequest;
use ic_nns_handler_root::{
    PROXIED_CANISTER_CALLS_TRACKER, encode_metrics, init::RootCanisterInitPayloadBuilder,
};
use ic_nns_test_utils::itest_helpers::{
    forward_call_via_universal_canister, set_up_root_canister, set_up_universal_canister,
    state_machine_test_on_nns_subnet,
};
use ic_test_utilities::universal_canister::UNIVERSAL_CANISTER_WASM;
use maplit::btreeset;
use pretty_assertions::assert_eq;
use std::{collections::BTreeSet, str::FromStr};

/// Verifies that an anonymous user can get the status of any NNS canister
/// through the root handler.
#[test]
fn test_get_status() {
    state_machine_test_on_nns_subnet(|runtime| async move {
        let root =
            set_up_root_canister(&runtime, RootCanisterInitPayloadBuilder::new().build()).await;

        // Create some NNS canister to be own by the root
        let universal = set_up_universal_canister(&runtime).await;
        universal
            .set_controller(root.canister_id().get())
            .await
            .unwrap();

        // Get the status of an NNS canister
        let status: CanisterStatusResult = root
            .update_(
                "canister_status",
                candid,
                (CanisterIdRecord::from(universal.canister_id()),),
            )
            .await
            .unwrap();
        assert_eq!(status.settings.controllers, vec![root.canister_id().get()]);

        Ok(())
    });
}
```
