### Title
Permanent Slot Leak via `.unwrap()` Trap in Public `canister_status` Endpoint — (`rs/nns/handlers/root/impl/canister/canister.rs`)

---

### Summary

The public `canister_status` endpoint in NNS Root calls `.unwrap()` on the management canister response without error handling. When the management canister rejects the call (e.g., because NNS Root does not control the target canister), the callback traps. Due to the IC's atomic callback execution model, the `SlotLoan` restoration that occurs before the trap is rolled back, permanently decrementing `AVAILABLE_MANAGEMENT_CANISTER_CALL_SLOT_COUNT`. An unprivileged attacker can drain all 167 slots with 167 sequential calls, after which the zero-slot guard blocks **all** management canister operations through NNS Root — including VIP (NNS Governance) operations.

---

### Finding Description

**Entrypoint:** The `canister_status` endpoint is a public `#[update]` with no caller restriction: [1](#0-0) 

**Slot accounting:** `new_management_canister_client()` wraps the real client in `LimitedOutstandingCallsManagementCanisterClient`, which decrements `AVAILABLE_MANAGEMENT_CANISTER_CALL_SLOT_COUNT` via a `SlotLoan` RAII guard and restores it on `Drop`: [2](#0-1) [3](#0-2) 

**The race between Drop and trap:** The execution splits across two IC message rounds:

- **Round 1** — slot decremented (167→166), inter-canister call sent to management canister. Round 1 commits.
- **Round 2 (callback)** — management canister returns `Err` (NNS Root is not a controller). `_loan` is dropped (166→167), then `.unwrap()` traps. The IC rolls back all Round 2 state changes, including the slot restoration. Net committed state: slot count = 166.

Each attack call permanently decrements the count by 1. After 167 calls the count reaches 0.

**Zero-slot guard blocks VIPs too:** The guard in `try_borrow_slot` returns `Err` for **all** callers when the count is zero, including NNS canisters (VIPs): [4](#0-3) 

VIPs have `used_slot_count = 0` but the `if *available_slot_count == 0` check fires before the subtraction, blocking them regardless.

**Slot initial value:** [5](#0-4) 

---

### Impact Explanation

Once all 167 slots are drained, every call to `new_management_canister_client()` inside NNS Root returns `Err(SysTransient, "Unavailable")`. This blocks:
- `change_nns_canister` (NNS Governance → NNS Root → management canister)
- `add_nns_canister`
- `stop_or_start_nns_canister`
- `update_canister_settings`
- `take_canister_snapshot` / `load_canister_snapshot`

All NNS Governance proposals that require management canister operations through NNS Root will fail until the canister is upgraded (which itself requires a governance proposal that may also fail). The canister state is persistent; the slot count does not self-recover.

---

### Likelihood Explanation

The attack requires no privileges, no cycles beyond ingress fees, and no coordination. The `canister_status` endpoint is publicly documented in `root.did`. Any principal can call it with an arbitrary `CanisterIdRecord`. 167 sequential ingress calls suffice; concurrency is not required. [6](#0-5) 

---

### Recommendation

Replace the `.unwrap()` with explicit error handling that returns an error to the caller without trapping:

```rust
#[update]
async fn canister_status(canister_id_record: CanisterIdRecord) -> Result<CanisterStatusResult, String> {
    let client = new_management_canister_client();
    client
        .canister_status(canister_id_record)
        .await
        .map(CanisterStatusResult::from)
        .map_err(|(code, msg)| format!("Management canister error {code}: {msg}"))
}
```

This ensures the `SlotLoan` drop and the function return happen in the same Round 2 execution without a trap, so the slot is always restored on commit.

---

### Proof of Concept

State-machine test outline:
1. Deploy NNS Root canister.
2. Create a canister **not** controlled by NNS Root.
3. Call `canister_status` on NNS Root with that canister's ID from the anonymous principal.
4. Assert the call returns a replica-level error (trap propagated as rejection).
5. Read `AVAILABLE_MANAGEMENT_CANISTER_CALL_SLOT_COUNT` via metrics or a test hook; assert it is 166.
6. Repeat steps 3–5 a total of 167 times; assert count reaches 0.
7. Call `change_nns_canister` from NNS Governance; assert it fails with `SysTransient / Unavailable`.

### Citations

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

**File:** rs/nervous_system/clients/src/management_canister_client.rs (L264-280)
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
```

**File:** rs/nervous_system/clients/src/management_canister_client.rs (L295-301)
```rust
    async fn canister_status(
        &self,
        canister_id_record: CanisterIdRecord,
    ) -> Result<CanisterStatusResultFromManagementCanister, (i32, String)> {
        let _loan = self.try_borrow_slot()?;
        self.inner.canister_status(canister_id_record).await
    }
```

**File:** rs/nervous_system/clients/src/management_canister_client.rs (L359-372)
```rust
/// Increments available_slot_count by used_slot_count when dropped.
struct SlotLoan {
    available_slot_count: &'static LocalKey<RefCell<u64>>,
    used_slot_count: u64,
}

impl Drop for SlotLoan {
    fn drop(&mut self) {
        self.available_slot_count
            .with_borrow_mut(|available_slot_count| {
                *available_slot_count = available_slot_count.saturating_add(self.used_slot_count);
            });
    }
}
```

**File:** rs/nns/handlers/root/impl/canister/root.did (L222-224)
```text
service : () -> {
  canister_status : (CanisterIdRecord) -> (CanisterStatusResult);
  get_build_metadata : () -> (text) query;
```
