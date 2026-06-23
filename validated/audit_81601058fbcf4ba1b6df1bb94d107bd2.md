### Title
`SlotLoan` Slot Permanently Lost via `canister_status` Panic-on-Unwrap After Callback Rollback — (`rs/nns/handlers/root/impl/canister/canister.rs`, `rs/nervous_system/clients/src/management_canister_client.rs`)

---

### Summary

The publicly accessible `canister_status` update method on the NNS root canister calls `.unwrap()` on the management canister result after an `await`. When the management canister rejects (because root is not a controller of the target), the `.unwrap()` panics, triggering a Wasm trap. The IC rolls back all state changes from the callback — including the `SlotLoan::drop` that had already incremented `available_slot_count` — permanently losing one slot. An unprivileged caller can repeat this 167 times to exhaust all slots, blocking all subsequent management canister calls through the root canister.

---

### Finding Description

**Step 1 — Slot is decremented and committed.**

In `LimitedOutstandingCallsManagementCanisterClient::canister_status`:

```rust
async fn canister_status(&self, ...) -> Result<...> {
    let _loan = self.try_borrow_slot()?;          // decrements available_slot_count
    self.inner.canister_status(canister_id_record).await  // <-- await point
}
```

`try_borrow_slot` decrements `available_slot_count` before the `.await`. On the IC, state is committed at every inter-canister call boundary. The decremented value is now durable. [1](#0-0) 

**Step 2 — Callback runs; `_loan` is dropped; then `.unwrap()` panics.**

The outer `canister_status` handler in the root canister:

```rust
#[update]
async fn canister_status(canister_id_record: CanisterIdRecord) -> CanisterStatusResult {
    let client = new_management_canister_client();
    let canister_status_response = client
        .canister_status(canister_id_record)
        .await
        .map(CanisterStatusResult::from);
    canister_status_response.unwrap()   // <-- panics on Err
}
```

When the management canister rejects (root is not a controller), the entire callback executes as one atomic unit:
1. `LimitedOutstandingCallsManagementCanisterClient::canister_status` returns `Err(...)`, dropping `_loan` (incrementing `available_slot_count`)
2. `.map(CanisterStatusResult::from)` keeps the `Err`
3. `.unwrap()` panics → Wasm trap

The IC rolls back all state changes from the callback to the pre-callback snapshot. That snapshot already has `available_slot_count` decremented. The increment from `_loan`'s drop is undone. The slot is permanently lost. [2](#0-1) [3](#0-2) 

**Step 3 — `available_slot_count` starts at 167; VIPs are also blocked at 0.**

```rust
static AVAILABLE_MANAGEMENT_CANISTER_CALL_SLOT_COUNT: RefCell<u64> = const { RefCell::new(167) };
```

The `try_borrow_slot` guard blocks **all** callers (including VIPs) when `available_slot_count == 0`:

```rust
if *available_slot_count == 0 {
    return Err((RejectCode::SysTransient as i32, "Unavailable...".to_string()));
}
```

VIPs (NNS canisters) have `used_slot_count = 0` so they don't decrement the counter, but they are still rejected when it reaches zero. [4](#0-3) [5](#0-4) 

**Step 4 — Existing test confirms the failure path is reachable but does not check the slot counter.**

`test_canister_status_call_tracking` explicitly tests calling `canister_status` on a canister root does not control, confirms it returns an error, and checks only the `ProxiedCanisterCallsTracker` metric — it never asserts that `AVAILABLE_MANAGEMENT_CANISTER_CALL_SLOT_COUNT` is unchanged. [6](#0-5) 

---

### Impact Explanation

After 167 calls to `canister_status` with any canister ID that root does not control, `available_slot_count` reaches 0. All subsequent calls to `canister_status`, `update_settings`, `stop_canister`, `delete_canister`, `take_canister_snapshot`, and `load_canister_snapshot` through the NNS root canister return `Err(SysTransient)` — including calls originating from NNS governance proposals. This blocks NNS canister management operations at the governance level.

Recovery requires upgrading the root canister (which resets the `thread_local!` to 167), but that upgrade itself requires a governance proposal that exercises the root canister's management path — creating a potential operational deadlock depending on which operations are needed.

---

### Likelihood Explanation

The `canister_status` endpoint has no authorization check and is callable by any anonymous or authenticated principal. The attack requires only 167 update calls, each costing a small amount of cycles. The management canister reliably rejects `canister_status` for any canister root does not control, making the panic deterministic. This is low-cost, fully automated, and requires no privileged access.

---

### Recommendation

1. **Immediate**: Replace `.unwrap()` with proper error propagation in `canister_status` (and `stop_or_start_nns_canister` which also has `.unwrap()` after await, though that one is governance-gated):
   ```rust
   canister_status_response.map_err(|e| trap(&format!("canister_status failed: {:?}", e)))
   ```
   Or return `Result<CanisterStatusResult, ...>` from the endpoint.

2. **Structural**: Move `SlotLoan` restoration out of the callback by storing the loan in stable/heap state keyed by call ID, and restoring it in a cleanup path that survives rollback — or use a pre-response hook. Alternatively, initialize `available_slot_count` from stable memory on `post_upgrade` so it can be reset without a full canister reinstall.

3. **Test**: Add an integration test that calls `canister_status` with a non-controlled canister N times and asserts `AVAILABLE_MANAGEMENT_CANISTER_CALL_SLOT_COUNT` is unchanged.

---

### Proof of Concept

```
for i in 1..=167:
    call NNS_ROOT_CANISTER.canister_status(
        canister_id = <any canister not controlled by root>
    )
    # Returns error (root not a controller), but each call permanently
    # decrements available_slot_count by 1 due to panic-on-unwrap + state rollback

# Now available_slot_count == 0
call NNS_ROOT_CANISTER.canister_status(any_canister_id)
# Returns: Err(SysTransient, "Unavailable. Maybe, try again later?")
# All management canister operations through root are now blocked.
```

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

**File:** rs/nervous_system/clients/src/management_canister_client.rs (L365-372)
```rust
impl Drop for SlotLoan {
    fn drop(&mut self) {
        self.available_slot_count
            .with_borrow_mut(|available_slot_count| {
                *available_slot_count = available_slot_count.saturating_add(self.used_slot_count);
            });
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

**File:** rs/nns/handlers/root/impl/tests/test_open_calls_tracking.rs (L17-62)
```rust
/// Test that the canister_status failure does not cause an incorrect increase
/// in the `nns_root_open_canister_status_calls_count` metric.
#[test]
fn test_canister_status_call_tracking() {
    // Setup the test
    let nns_init_payload = NnsInitPayloadsBuilder::new().build();
    let machine = StateMachine::new();
    setup_nns_canisters(&machine, nns_init_payload);

    // Create a test canister without setting NNS Root as controller.
    let universal = set_up_universal_canister(&machine, None);

    // Canister status call should fail as NNS Root is not a controller.
    assert!(
        update_with_sender::<_, CanisterStatusResult>(
            &machine,
            ROOT_CANISTER_ID,
            "canister_status",
            CanisterIdRecord::from(universal),
            PrincipalId::new_anonymous(),
        )
        .is_err()
    );

    // Queries the HTTP metrics endpoint.
    let response_bytes = query(
        &machine,
        ROOT_CANISTER_ID,
        "http_request",
        Encode!(&HttpRequest {
            url: "/metrics".to_string(),
            method: "GET".to_string(),
            headers: vec![],
            body: ByteBuf::new(),
        })
        .unwrap(),
    )
    .unwrap();
    let response: HttpResponse = Decode!(&response_bytes, HttpResponse).unwrap();
    let response_body = String::from_utf8(response.body.into_vec()).unwrap();

    // If there is any in-flight proxied canister call, the below string will
    // appear in the response body, like:
    // nns_root_in_flight_proxied_canister_call_count{method_name="canister_status",caller="...",callee="..."} 1
    assert!(!response_body.contains("nns_root_in_flight_proxied_canister_call_count{method_name="));
}
```
