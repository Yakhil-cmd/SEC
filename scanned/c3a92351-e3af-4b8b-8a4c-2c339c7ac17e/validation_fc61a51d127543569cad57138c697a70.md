### Title
VIP Callers Blocked by Non-VIP Slot Exhaustion in `try_borrow_slot` — (`rs/nervous_system/clients/src/management_canister_client.rs`)

---

### Summary

A logic error in `LimitedOutstandingCallsManagementCanisterClient::try_borrow_slot` causes VIP callers (NNS canisters) to be rejected with `SysTransient` when the shared slot pool is exhausted by non-VIP callers, despite VIP callers being designed to consume zero slots. An unprivileged canister can exploit the publicly-accessible `canister_status` endpoint on NNS Root to exhaust all 167 slots, blocking governance-triggered `take_canister_snapshot` and `load_canister_snapshot` operations.

---

### Finding Description

**Root cause — off-by-one guard in `try_borrow_slot`:**

The function computes `used_slot_count = 0` for VIP callers, correctly meaning they should not consume a slot. However, the exhaustion guard fires unconditionally before the subtraction:

```rust
fn try_borrow_slot(&self) -> Result<SlotLoan, (i32, String)> {
    let used_slot_count = if self.is_caller_vip { 0 } else { 1 };  // VIP = 0

    self.available_slot_count
        .with_borrow_mut(|available_slot_count| {
            if *available_slot_count == 0 {          // ← blocks ALL callers, including VIP
                let code = RejectCode::SysTransient as i32;
                return Err((code, message));
            }
            *available_slot_count = available_slot_count.saturating_sub(used_slot_count);
            Ok(())
        })?;
``` [1](#0-0) 

When `available_slot_count` reaches 0, a VIP caller with `used_slot_count = 0` would not change the counter at all — but it never reaches the subtraction because the guard returns `Err` first.

**Attacker-controlled entrypoint — public `canister_status`:**

NNS Root exposes `canister_status` as an unrestricted `#[update]` endpoint. The docstring explicitly states it is public:

```rust
/// The status of NNS canisters should be public information: anyone can get the
/// status of any NNS canister.
#[update]
async fn canister_status(canister_id_record: CanisterIdRecord) -> CanisterStatusResult {
    let client = new_management_canister_client();
    ...
}
``` [2](#0-1) 

Each call to this endpoint borrows a slot for the full round-trip duration of the inner management canister call.

**Slot pool is shared and fixed at 167:**

```rust
thread_local! {
    // How this value was chosen: queues become full at 500. This is 1/3 of that.
    static AVAILABLE_MANAGEMENT_CANISTER_CALL_SLOT_COUNT: RefCell<u64> = const { RefCell::new(167) };
}
``` [3](#0-2) 

**VIP determination happens per-call in `new_management_canister_client`:**

```rust
let is_caller_vip = CanisterId::try_from(caller())
    .map(|caller| ALL_NNS_CANISTER_IDS.contains(&&caller))
    .unwrap_or(false);
``` [4](#0-3) 

Governance calling `take_canister_snapshot` or `load_canister_snapshot` correctly gets `is_caller_vip = true`, but `try_borrow_slot` still rejects it when the pool is at 0. [5](#0-4) 

---

### Impact Explanation

When all 167 slots are occupied by non-VIP in-flight calls, any governance-triggered call to `take_canister_snapshot` or `load_canister_snapshot` on NNS Root returns `Err(SysTransient, "Unavailable. Maybe, try again later?")`. This directly blocks disaster recovery operations (snapshot/restore of ledger or governance canisters) for the duration of the attack. If a time-sensitive rollback is required (e.g., to recover from a corrupted ledger state), the attacker can sustain the denial by continuously replenishing the 167 in-flight calls.

---

### Likelihood Explanation

The IC execution model allows NNS Root to have up to 167 concurrent in-flight inter-canister calls simultaneously (one per message, suspended at the `await` point). An attacker canister can send 167 `canister_status` messages to NNS Root in rapid succession; NNS Root processes each synchronously up to the `await`, suspends, and moves to the next — all 167 management canister calls are in-flight simultaneously until the management canister responds (next round). The attacker needs only cycles to sustain the attack. No privileged access, no key material, no social engineering is required.

---

### Recommendation

In `try_borrow_slot`, skip the exhaustion guard entirely for VIP callers, since they consume zero slots and cannot deplete the pool:

```rust
fn try_borrow_slot(&self) -> Result<SlotLoan, (i32, String)> {
    let used_slot_count = if self.is_caller_vip { 0 } else { 1 };

    self.available_slot_count
        .with_borrow_mut(|available_slot_count| {
            if !self.is_caller_vip && *available_slot_count == 0 {
                let code = RejectCode::SysTransient as i32;
                return Err((code, "Unavailable. Maybe, try again later?".to_string()));
            }
            *available_slot_count = available_slot_count.saturating_sub(used_slot_count);
            Ok(())
        })?;
    ...
}
``` [6](#0-5) 

---

### Proof of Concept

State-machine test outline:

1. Deploy an attacker canister on the NNS subnet.
2. Have the attacker canister send 167 concurrent inter-canister calls to NNS Root's `canister_status`, each targeting a canister NNS Root controls (e.g., governance), with a mock inner client that stalls before responding.
3. Assert `AVAILABLE_MANAGEMENT_CANISTER_CALL_SLOT_COUNT == 0`.
4. From the governance canister (VIP), call `load_canister_snapshot` on NNS Root.
5. Assert the response is `Err` with reject code `SysTransient` — confirming VIP is blocked despite `is_caller_vip = true` and `used_slot_count = 0`.

The existing unit test in `tests.rs` already demonstrates the VIP-bypass intent (VIP calls succeed even when non-VIP slots are full with capacity=2), but it does not test the `available_slot_count == 0` edge case where the guard fires before the VIP path is reached. [7](#0-6)

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

**File:** rs/nns/handlers/root/impl/canister/canister.rs (L47-51)
```rust
thread_local! {
    // How this value was chosen: queues become full at 500. This is 1/3 of that, which seems to be
    // a reasonable balance.
    static AVAILABLE_MANAGEMENT_CANISTER_CALL_SLOT_COUNT: RefCell<u64> = const { RefCell::new(167) };
}
```

**File:** rs/nns/handlers/root/impl/canister/canister.rs (L57-66)
```rust
    // Here, VIP = is an NNS canister
    let is_caller_vip = CanisterId::try_from(caller())
        .map(|caller| ALL_NNS_CANISTER_IDS.contains(&&caller))
        .unwrap_or(false);

    LimitedOutstandingCallsManagementCanisterClient::new(
        client,
        &AVAILABLE_MANAGEMENT_CANISTER_CALL_SLOT_COUNT,
        is_caller_vip,
    )
```

**File:** rs/nns/handlers/root/impl/canister/canister.rs (L81-98)
```rust
/// Returns the status of the canister specified in the input.
///
/// The status of NNS canisters should be public information: anyone can get the
/// status of any NNS canister.
///
/// This must be an update, not a query, because an inter-canister call to the
/// management canister is required.
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

**File:** rs/nns/handlers/root/impl/canister/canister.rs (L244-268)
```rust
#[update]
async fn take_canister_snapshot(
    take_canister_snapshot_request: TakeCanisterSnapshotRequest,
) -> TakeCanisterSnapshotResponse {
    check_caller_is_governance();
    ic_nervous_system_root::take_canister_snapshot::take_canister_snapshot(
        take_canister_snapshot_request,
        new_management_canister_client(),
    )
    .await
}

/// Loads a snapshot of a canister controlled by NNS Root. Only callable by NNS
/// Governance.
#[update]
async fn load_canister_snapshot(
    load_canister_snapshot_request: LoadCanisterSnapshotRequest,
) -> LoadCanisterSnapshotResponse {
    check_caller_is_governance();
    ic_nervous_system_root::load_canister_snapshot::load_canister_snapshot(
        load_canister_snapshot_request,
        new_management_canister_client(),
    )
    .await
}
```

**File:** rs/nervous_system/clients/src/management_canister_client/tests.rs (L173-201)
```rust
    let results = futures::future::join_all(vec![
        // Listed in order of start time (i.e. pre_flight_pause_duration); whereas, end times could
        // be all over the place.

        // Servicing requests where the caller is a VIP. These are suppoed to not occupy call slots.
        canister_status(
            true,                       // is_caller_vip
            Duration::from_millis(0),   // pre_flight_pause_duration
            Duration::from_millis(500), // inner_duration
            Some(Ok(base_canister_status_result.clone())),
        ),
        canister_status(
            true,
            Duration::from_millis(0),
            Duration::from_millis(500),
            Some(Ok(base_canister_status_result.clone())),
        ),
        canister_status(
            true,
            Duration::from_millis(0),
            Duration::from_millis(500),
            Some(Ok(base_canister_status_result.clone())),
        ),
        canister_status(
            true,
            Duration::from_millis(0),
            Duration::from_millis(500),
            Some(Ok(base_canister_status_result.clone())),
        ),
```
