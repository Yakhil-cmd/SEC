Audit Report

## Title
VIP Caller Bypass Failure in `try_borrow_slot` Allows Slot Exhaustion to Block NNS Governance Operations — (File: `rs/nervous_system/clients/src/management_canister_client.rs`)

## Summary
`LimitedOutstandingCallsManagementCanisterClient::try_borrow_slot` unconditionally rejects all callers when `available_slot_count == 0`, including VIP callers (NNS canisters such as Governance) that are designed to consume zero slots. An unprivileged attacker can exhaust all 167 slots via the unauthenticated `canister_status` endpoint on NNS root, blocking Governance from executing `update_canister_settings`, `change_canister_controllers`, `take_canister_snapshot`, and `load_canister_snapshot` for the duration of the attack.

## Finding Description
In `rs/nervous_system/clients/src/management_canister_client.rs` at line 265, `used_slot_count` is correctly set to `0` for VIP callers and `1` for non-VIP callers. However, the guard at line 269 — `if *available_slot_count == 0 { return Err(...) }` — fires unconditionally for every caller before `used_slot_count` is consulted. A VIP caller would subtract 0 from the count (line 278, `saturating_sub(0)`), leaving it unchanged, but the early return at line 275 prevents this path from ever being reached when the count is zero.

The unauthenticated `canister_status` endpoint in `rs/nns/handlers/root/impl/canister/canister.rs` (lines 88–98) has no caller check and calls `new_management_canister_client()` with `is_caller_vip = false` for any non-NNS caller. Each in-flight call holds a slot until the management canister responds. With 167 concurrent attacker-controlled calls, the shared `AVAILABLE_MANAGEMENT_CANISTER_CALL_SLOT_COUNT` reaches zero. Subsequent calls from NNS Governance (which correctly sets `is_caller_vip = true`) are then rejected with `SysTransient / "Unavailable. Maybe, try again later?"` — the same error as non-VIP callers — despite Governance being entitled to bypass the limit.

Additionally, `canister_status` at line 97 calls `.unwrap()` on the result, meaning a slot-exhausted non-VIP call traps the canister message rather than returning a graceful error to the caller.

## Impact Explanation
This is a targeted, application-level DoS against NNS Governance operations. While slots are exhausted, Governance cannot execute `update_canister_settings`, `change_canister_controllers`, `take_canister_snapshot`, or `load_canister_snapshot` — all critical NNS operations. Blocking these during a governance proposal execution window (e.g., an NNS canister upgrade) constitutes a meaningful disruption of NNS availability. This matches the allowed High impact: "Application/platform-level DoS, crash, consensus blocking, certified-state disruption, or subnet availability impact not based on raw volumetric DDoS."

## Likelihood Explanation
`canister_status` is publicly callable by any principal with no per-principal rate limiting at the application layer. Holding 167 concurrent in-flight calls is well within the IC's per-canister queue limit of 500. Management canister `canister_status` calls are cross-subnet round-trips spanning multiple consensus rounds, giving the attacker a multi-second window per batch. The attacker can sustain the attack by continuously submitting new batches as slots free up, requiring no special privileges, no victim interaction, and no external dependencies.

## Recommendation
Modify the guard in `try_borrow_slot` to skip rejection for VIP callers:

```rust
if *available_slot_count == 0 && !self.is_caller_vip {
    let code = RejectCode::SysTransient as i32;
    let message = "Unavailable. Maybe, try again later?".to_string();
    return Err((code, message));
}
```

Additionally, replace the `.unwrap()` at line 97 of `canister.rs` with proper error propagation to avoid trapping the canister message on slot exhaustion.

## Proof of Concept
1. Install the NNS root canister in a PocketIC or state-machine test environment.
2. Submit 167 concurrent `canister_status` calls from an anonymous principal, each targeting a valid canister ID, and hold them in-flight (management canister calls will be pending for at least one consensus round).
3. While those calls are in-flight, submit `update_canister_settings` from the NNS Governance canister principal.
4. Assert the Governance call returns `SysTransient` / "Unavailable. Maybe, try again later?" — proving VIP callers are incorrectly blocked.
5. Wait for the 167 calls to complete (slots freed), resubmit `update_canister_settings` from Governance, and assert it succeeds.
The existing unit test in `rs/nervous_system/clients/src/management_canister_client/tests.rs` already demonstrates slot-exhaustion behavior for non-VIP callers; extending it with a VIP caller at count=0 directly proves the bug.