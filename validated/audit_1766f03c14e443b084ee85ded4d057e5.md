Audit Report

## Title
VIP Caller Bypass Incomplete in `try_borrow_slot`: Slot Exhaustion Blocks Governance Operations — (`rs/nervous_system/clients/src/management_canister_client.rs`)

## Summary

`LimitedOutstandingCallsManagementCanisterClient::try_borrow_slot` sets `used_slot_count = 0` for VIP (NNS canister) callers, preventing them from depleting the slot pool, but the `if *available_slot_count == 0` guard is evaluated unconditionally before the subtraction. Any unprivileged principal can exhaust all 167 slots via the public `canister_status` endpoint, causing the zero-check to reject even governance callers with `SysTransient`, blocking `update_canister_settings`, `take_canister_snapshot`, and `load_canister_snapshot` for the duration of the attack.

## Finding Description

In `rs/nervous_system/clients/src/management_canister_client.rs` at lines 264–280, `try_borrow_slot` computes `used_slot_count = 0` for VIP callers (line 265) but then enters the same `with_borrow_mut` closure that checks `if *available_slot_count == 0` (line 269) before any subtraction occurs. When the pool is at zero, this branch returns `Err((SysTransient, ...))` for every caller regardless of VIP status. The VIP path only matters at line 278 (`saturating_sub(used_slot_count)`), which is never reached when the pool is empty.

The public `canister_status` endpoint at `rs/nns/handlers/root/impl/canister/canister.rs` lines 88–98 carries no authorization check and calls `new_management_canister_client()`, which shares the same `AVAILABLE_MANAGEMENT_CANISTER_CALL_SLOT_COUNT` thread-local (initialized to 167 at line 50). An attacker spawning 167 concurrent `canister_status` calls against a slow-responding target canister holds all slots for the duration of the management canister round-trips.

While those 167 calls are in-flight, governance calls to `update_canister_settings` (line 221–230), `take_canister_snapshot` (lines 244–254), and `load_canister_snapshot` (lines 258–268) each invoke `new_management_canister_client()` and immediately hit the zero-check, returning `SysTransient` to the governance canister. The attack is sustainable by continuously replenishing calls as slots free up.

`stop_or_start_nns_canister` (lines 171–179 of `canister_management.rs`) and `change_nns_canister` are not affected because they bypass `LimitedOutstandingCallsManagementCanisterClient` entirely.

## Impact Explanation

This is an application-level DoS against NNS governance execution. Governance proposals requiring `update_canister_settings`, `take_canister_snapshot`, or `load_canister_snapshot` will fail with `SysTransient` for the entire attack window. The attack requires no credentials, no key material, and no consensus corruption. It matches the allowed impact: **"Application/platform-level DoS … or subnet availability impact not based on raw volumetric DDoS"** — severity **High ($2,000–$10,000)**. The primary NNS canister upgrade path (`change_nns_canister`) is unaffected, which bounds the severity below Critical.

## Likelihood Explanation

The `canister_status` endpoint accepts calls from any principal including anonymous. The IC input queue holds up to 500 messages; 167 concurrent callers is well within reach for a single coordinated actor or small botnet. Each call awaits a management canister round-trip, creating a multi-round window. The attack can be sustained indefinitely by re-submitting calls as slots are released, requiring no ongoing privileged access.

## Recommendation

Move the VIP check before the zero-guard so VIP callers bypass it entirely:

```rust
fn try_borrow_slot(&self) -> Result<SlotLoan, (i32, String)> {
    if self.is_caller_vip {
        return Ok(SlotLoan {
            available_slot_count: self.available_slot_count,
            used_slot_count: 0,
        });
    }
    self.available_slot_count
        .with_borrow_mut(|available_slot_count| {
            if *available_slot_count == 0 {
                let code = RejectCode::SysTransient as i32;
                return Err((code, "Unavailable. Maybe, try again later?".to_string()));
            }
            *available_slot_count -= 1;
            Ok(())
        })?;
    ...
}
```

Alternatively, reserve a dedicated sub-pool of slots for VIP callers that non-VIP callers cannot consume.

## Proof of Concept

Using the existing test infrastructure in `rs/nervous_system/clients/src/management_canister_client/tests.rs`:

1. Instantiate `LimitedOutstandingCallsManagementCanisterClient` with capacity 2 and a mock inner client whose `canister_status` sleeps for a controlled duration.
2. Spawn 2 concurrent non-VIP `canister_status` calls (slots → 0); do not await them yet.
3. While both are in-flight, invoke `canister_status` (or `update_settings`) with `is_caller_vip = true`.
4. Assert the VIP call returns `Err((SysTransient, "Unavailable. Maybe, try again later?"))` — proving the invariant violation.
5. Await the 2 in-flight calls; retry the VIP call and assert it succeeds.

Scaling to 167 slots with the production `AVAILABLE_MANAGEMENT_CANISTER_CALL_SLOT_COUNT` and the real `canister_status` endpoint directly reproduces the governance-blocking scenario.