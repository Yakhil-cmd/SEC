Audit Report

## Title
VIP Slot Bypass Broken in `try_borrow_slot`: NNS Root VIP Callers Blocked When Slot Pool Exhausted — (`rs/nervous_system/clients/src/management_canister_client.rs`)

## Summary

`LimitedOutstandingCallsManagementCanisterClient::try_borrow_slot` contains an unconditional zero-guard that rejects all callers — including VIPs — when `available_slot_count == 0`. The VIP flag only suppresses slot consumption (`used_slot_count = 0`) but does not bypass the early-return guard. An unprivileged attacker can exhaust all 167 slots with concurrent `canister_status` ingress calls, after which NNS canisters (SNS-W, Governance) calling `change_canister_controllers` or `update_canister_settings` through NNS Root receive `SysTransient` errors and fail.

## Finding Description

**Root cause:** In `rs/nervous_system/clients/src/management_canister_client.rs` at lines 264–287, `try_borrow_slot` computes `used_slot_count = if self.is_caller_vip { 0 } else { 1 }`, but the guard `if *available_slot_count == 0 { return Err(...) }` at line 269 is unconditional. When the pool is zero, VIP callers hit this guard and are rejected identically to non-VIP callers.

**Exploit path:**

1. `canister_status` in `rs/nns/handlers/root/impl/canister/canister.rs` (lines 88–98) is an open `#[update]` with no access control. Any caller can invoke it.
2. Each call goes through `new_management_canister_client()` (lines 53–67), which sets `is_caller_vip = false` for non-NNS callers, and creates a `LimitedOutstandingCallsManagementCanisterClient` backed by `AVAILABLE_MANAGEMENT_CANISTER_CALL_SLOT_COUNT` (initialized to 167 at line 50).
3. An attacker sends 167 concurrent `canister_status` ingress messages. Each decrements the pool by 1 (167 → 0) while awaiting the management canister response.
4. SNS-W calls `change_canister_controllers` (lines 206–216), which calls `new_management_canister_client()` with `is_caller_vip = true`. Inside, `try_borrow_slot` hits `available_slot_count == 0` at line 269 and returns `Err(SysTransient, "Unavailable. Maybe, try again later?")` — despite `is_caller_vip = true`.
5. Same applies to Governance calling `update_canister_settings` (lines 220–230).
6. `SlotLoan::drop` (lines 365–372) adds back `used_slot_count` on drop. For VIPs, `used_slot_count = 0`, so no slots are returned — but VIPs never successfully borrow a slot either, making the VIP design entirely non-functional when the pool is at zero.

**Why existing checks fail:** The VIP mechanism was designed to allow NNS canisters to bypass slot consumption. The implementation correctly sets `used_slot_count = 0` for VIPs, but the zero-guard precedes the subtraction and is not conditioned on `is_caller_vip`, defeating the entire VIP bypass.

## Impact Explanation

NNS Root becomes temporarily unable to process management canister calls on behalf of VIP NNS canisters. `change_canister_controllers` (SNS deployment flow) and `update_canister_settings` (Governance-controlled canister settings) fail with transient errors for the duration of the attack. Dapp canisters being transferred to SNS control can be left stranded under NNS Root with no recovery path until the attacker stops replenishing calls. This matches the **High** impact category: application/platform-level DoS with concrete harm to NNS/SNS governance operations, not based on raw volumetric DDoS.

## Likelihood Explanation

The `canister_status` endpoint requires no authentication, no cycles beyond normal ingress, and no privileged access. Sending 167 concurrent ingress messages is well within IC ingress queue limits (500 per canister). The attacker must continuously replenish calls to maintain exhaustion (since `SlotLoan::drop` returns slots), but this is a low-cost, repeatable operation requiring no special capability. The attack is fully automatable by any external user.

## Recommendation

Condition the zero-guard on `!self.is_caller_vip` in `try_borrow_slot`:

```rust
fn try_borrow_slot(&self) -> Result<SlotLoan, (i32, String)> {
    let used_slot_count = if self.is_caller_vip { 0 } else { 1 };

    self.available_slot_count
        .with_borrow_mut(|available_slot_count| {
            if *available_slot_count == 0 && !self.is_caller_vip {
                let code = RejectCode::SysTransient as i32;
                return Err((code, "Unavailable. Maybe, try again later?".to_string()));
            }
            *available_slot_count = available_slot_count.saturating_sub(used_slot_count);
            Ok(())
        })?;
    ...
}
```

This ensures VIP callers always proceed regardless of pool state, while non-VIP callers remain rate-limited.

## Proof of Concept

State-machine or PocketIC test:

1. Deploy NNS canisters (NNS Root, SNS-W, Governance).
2. Send 167 concurrent `canister_status` update calls from `PrincipalId::new_anonymous()` to `ROOT_CANISTER_ID`, targeting a stopped canister to keep management canister responses pending and slots occupied.
3. While those calls are in-flight, have SNS-W call `change_canister_controllers` on NNS Root.
4. Assert the response is `Err` with reject code `SysTransient` and message containing "Unavailable" — confirming VIP bypass fails at zero pool.
5. Allow the 167 calls to complete (slots return via `SlotLoan::drop`), retry SNS-W's call, and assert it succeeds — confirming the DoS is slot-exhaustion-driven and not permanent.