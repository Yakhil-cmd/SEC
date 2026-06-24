### Title
Slot Leak via Wasm-Trap State Rollback Defeats `SlotLoan::drop` — Permanent Non-VIP DoS on NNS Root Management-Canister Proxying - (`rs/nns/handlers/root/impl/canister/canister.rs`, `rs/nervous_system/clients/src/management_canister_client.rs`)

---

### Summary

The NNS Root canister's `canister_status` update method calls `.unwrap()` on the management-canister result after the `SlotLoan` guard has already been dropped. On the Internet Computer, a Wasm trap (panic) causes the IC execution environment to **roll back all heap-state changes made during that callback execution**, including the `available_slot_count` increment written by `SlotLoan::drop`. The decrement written during the *preceding* execution (before the inter-canister call) was already committed and is never rolled back. The net effect is a permanent one-slot leak per attacker-triggered rejection. With a pool of 167 slots, 167 such calls permanently exhaust the pool.

---

### Finding Description

**Execution model background.** On the IC, an `async` canister function is split at every `await`-on-inter-canister-call boundary into separate message executions. State changes in each execution are committed atomically on success and rolled back entirely on trap. Rust's `Drop` glue is never invoked on a Wasm trap (Wasm traps abort without stack unwinding).

**Step-by-step trace:**

| Execution | What happens | State committed? |
|---|---|---|
| **Ingress execution** | `try_borrow_slot()` decrements `AVAILABLE_MANAGEMENT_CANISTER_CALL_SLOT_COUNT` by 1; inter-canister call to management canister is enqueued | **Yes** — slot is gone |
| **Callback execution** | Management canister rejects (NNS Root is not a controller of the target); `LimitedOutstandingCallsManagementCanisterClient::canister_status` returns `Err`, dropping `_loan` → counter incremented; then `.unwrap()` on `Err` panics → Wasm trap | **No** — entire callback rolled back, including the increment |

The decrement is in the committed ingress execution; the compensating increment is in the rolled-back callback execution. The slot is permanently gone.

**Concrete panic trigger.** The root canister's public `canister_status` endpoint has no access-control guard and calls `.unwrap()` unconditionally:

```rust
// rs/nns/handlers/root/impl/canister/canister.rs  line 97
canister_status_response.unwrap()
```

The management canister rejects `canister_status` whenever the caller (NNS Root) is not a controller of the requested canister. Any principal can supply an arbitrary `canister_id_record`, so any unprivileged user can force a rejection and therefore a trap.

**Pool size.** The pool is fixed at 167 slots:

```rust
// rs/nns/handlers/root/impl/canister/canister.rs  line 50
static AVAILABLE_MANAGEMENT_CANISTER_CALL_SLOT_COUNT: RefCell<u64> = const { RefCell::new(167) };
```

There is no reset path; the counter is only ever modified by `try_borrow_slot` (decrement) and `SlotLoan::drop` (increment). After 167 leaked calls the counter reaches 0 and every subsequent non-VIP call returns `SysTransient / "Unavailable"`.

---

### Impact Explanation

All non-VIP callers (everyone who is not an NNS canister) are permanently denied access to every management-canister-proxying endpoint on NNS Root: `canister_status`, `update_settings`, `canister_metadata`, `stop_canister`, `delete_canister`, `take_canister_snapshot`, `load_canister_snapshot`. VIP callers (NNS canisters) use `used_slot_count = 0` and are unaffected. The canister cannot be upgraded to fix the counter without a governance proposal, which itself takes days.

---

### Likelihood Explanation

- The `canister_status` endpoint is public, requires no cycles beyond the ingress fee, and has no rate-limiting beyond the slot guard itself.
- The attack requires exactly 167 sequential or concurrent ingress calls with a non-controlled canister ID.
- No privileged access, no key material, no social engineering, and no consensus-level attack is needed.
- The management canister's rejection of non-controller `canister_status` calls is deterministic and guaranteed by the IC protocol.

---

### Recommendation

1. **Remove the `.unwrap()`** in `canister_status` (line 97 of `canister.rs`). Return the error to the caller instead of trapping; this keeps the callback execution clean and allows `SlotLoan::drop` to commit normally.
2. **Alternatively**, move the slot borrow *outside* the async boundary — borrow the slot, make the call, and return the slot all within a single synchronous execution segment — so the decrement and increment are always in the same atomic execution.
3. **Defensively**, add a periodic or post-upgrade reset of `AVAILABLE_MANAGEMENT_CANISTER_CALL_SLOT_COUNT` to its initial value, or expose a governance-callable reset method.

---

### Proof of Concept

```
# 167 ingress calls, each with a canister_id that NNS Root does not control
for i in $(seq 1 167); do
  dfx canister call nns-root canister_status "(record { canister_id = principal \"aaaaa-aa\" })" &
done
wait

# Now every non-VIP canister_status call returns:
# Err((SysTransient, "Unavailable. Maybe, try again later?"))
dfx canister call nns-root canister_status "(record { canister_id = principal \"<any-nns-canister>\" })"
# → trapped / SysTransient
```

Differential assertion: record `AVAILABLE_MANAGEMENT_CANISTER_CALL_SLOT_COUNT` before and after a single error-path call; it will be `N-1` after, not `N`.

---

**Relevant code locations:**

- Slot decrement (committed): [1](#0-0) 
- `SlotLoan::drop` increment (rolled back on trap): [2](#0-1) 
- Panic trigger `.unwrap()` in callback: [3](#0-2) 
- Fixed pool of 167 slots, no reset path: [4](#0-3)

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

**File:** rs/nns/handlers/root/impl/canister/canister.rs (L89-98)
```rust
async fn canister_status(canister_id_record: CanisterIdRecord) -> CanisterStatusResult {
    let client = new_management_canister_client();

    let canister_status_response = client
        .canister_status(canister_id_record)
        .await
        .map(CanisterStatusResult::from);

    canister_status_response.unwrap()
}
```
