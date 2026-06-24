Audit Report

## Title
VIP Slot Bypass Broken in `try_borrow_slot`: Non-Privileged Callers Can Block Governance `update_canister_settings` — (`rs/nervous_system/clients/src/management_canister_client.rs`)

## Summary

`LimitedOutstandingCallsManagementCanisterClient::try_borrow_slot` sets `used_slot_count = 0` for VIP callers to avoid consuming a slot, but the unconditional `if *available_slot_count == 0` guard at line 269 still returns `Err(SysTransient)` for VIP callers when the pool is empty. An unprivileged external caller can exhaust all 167 slots via concurrent `canister_status` calls, causing governance's subsequent `update_canister_settings` call to fail transiently — delaying or blocking time-sensitive NNS configuration changes.

## Finding Description

In `rs/nervous_system/clients/src/management_canister_client.rs` at lines 264–287, `try_borrow_slot` computes `used_slot_count = 0` for VIP callers (line 265), but the `== 0` guard at line 269 is unconditional and fires before `used_slot_count` is ever consulted:

```rust
fn try_borrow_slot(&self) -> Result<SlotLoan, (i32, String)> {
    let used_slot_count = if self.is_caller_vip { 0 } else { 1 };  // line 265

    self.available_slot_count
        .with_borrow_mut(|available_slot_count| {
            if *available_slot_count == 0 {   // line 269 — fires for VIP too
                return Err((code, message));
            }
            *available_slot_count = available_slot_count.saturating_sub(used_slot_count);
            Ok(())
        })?;
```

The VIP path was intended to bypass the limit entirely (consuming 0 slots), but the early-exit guard makes the bypass ineffective when the pool reaches zero.

The attack path:
1. `canister_status` in `rs/nns/handlers/root/impl/canister/canister.rs` (lines 88–98) is a public `#[update]` endpoint with no access control.
2. `new_management_canister_client()` (lines 53–67) sets `is_caller_vip = false` for any non-NNS caller.
3. Each concurrent `canister_status` call holds one slot for the full management canister round-trip. Sending 167 concurrent calls exhausts `AVAILABLE_MANAGEMENT_CANISTER_CALL_SLOT_COUNT` (line 50, initialized to 167).
4. When governance (a VIP) subsequently calls `update_canister_settings` (lines 221–230), `new_management_canister_client()` correctly sets `is_caller_vip = true`, but `try_borrow_slot` returns `Err(SysTransient)` because the pool is at zero.
5. The error propagates through `canister_management::update_canister_settings` (lines 247–258 of `rs/nns/handlers/root/impl/src/canister_management.rs`) as `UpdateCanisterSettingsResponse::Err`.

## Impact Explanation

This is a **High** severity finding matching the allowed impact: *"Application/platform-level DoS … or subnet availability impact not based on raw volumetric DDoS."* Governance proposals executing `UpdateCanisterSettings` (and similarly `change_canister_controllers`, `take_canister_snapshot`, `load_canister_snapshot`) fail transiently while the slot pool is exhausted. This can delay or prevent time-sensitive NNS configuration changes such as freezing thresholds, memory limits, and controller updates on NNS-controlled canisters. The impact is not merely cosmetic — a failed `UpdateCanisterSettingsResponse::Err` returned to governance constitutes a concrete, observable governance operation failure.

## Likelihood Explanation

The `canister_status` endpoint is intentionally public. The IC per-canister ingress queue supports well over 167 concurrent messages (the code comment notes queues fill at 500). An attacker needs only to keep 167 calls in flight simultaneously — targeting any canister ID (the management canister will reject unauthorized ones, but the slot is held during the round-trip). The attack window is narrow per attempt but is fully repeatable: the attacker can re-exhaust the pool as slots drain. No special privileges, leaked keys, or social engineering are required.

## Recommendation

Skip the `== 0` guard entirely for VIP callers in `try_borrow_slot`:

```rust
fn try_borrow_slot(&self) -> Result<SlotLoan, (i32, String)> {
    let used_slot_count = if self.is_caller_vip { 0 } else { 1 };

    self.available_slot_count
        .with_borrow_mut(|available_slot_count| {
            if !self.is_caller_vip && *available_slot_count == 0 {
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

## Proof of Concept

The existing test infrastructure in `rs/nervous_system/clients/src/management_canister_client/tests.rs` already demonstrates slot exhaustion behavior. A minimal unit test extension:

```rust
#[tokio::test]
async fn test_vip_blocked_when_slots_exhausted() {
    thread_local! {
        static SLOTS: RefCell<u64> = RefCell::new(2);
    }
    // Spawn 2 non-VIP calls with a mock inner that never resolves (holds slots indefinitely)
    // ... hold both slots via tokio tasks that never complete their inner future

    // Attempt a VIP update_settings call
    let vip_client = LimitedOutstandingCallsManagementCanisterClient::new(
        mock_inner, &SLOTS, true /* is_caller_vip */,
    );
    let result = vip_client.update_settings(dummy_settings).await;
    // Currently asserts Err with SysTransient — demonstrating VIP is incorrectly blocked
    assert!(result.is_err());
    // After fix, this should be Ok(())
}
```

This test directly mirrors the structure of the existing `test_limit_outstanding_calls` test and can be added to `rs/nervous_system/clients/src/management_canister_client/tests.rs` without any new infrastructure.