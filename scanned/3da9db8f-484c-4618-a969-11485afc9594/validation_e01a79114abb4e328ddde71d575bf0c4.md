### Title
Unprivileged Griefing of Timer Recovery via Shared Global `last_reset_timestamp_seconds` — (`rs/sns/root/canister/canister.rs`, `rs/sns/governance/canister/canister.rs`, `rs/sns/swap/canister/canister.rs`)

---

### Summary

The `reset_timers` update method in SNS Root, SNS Governance, and SNS Swap canisters uses a single shared global `last_reset_timestamp_seconds` with no caller access control. Any unprivileged ingress sender can call `reset_timers`, which updates the global cooldown timestamp and blocks all other callers from invoking the same recovery function for the entire cooldown period (up to one week for SNS Root). This is a direct analog to H-01: a shared global reset timestamp that should be irrelevant to caller identity is instead used as a global gate, allowing any caller to grief legitimate recovery operations.

---

### Finding Description

The `reset_timers` function in all three SNS canisters is decorated with `#[update]` and carries **no caller identity check**. It reads and writes a single shared `last_reset_timestamp_seconds` field stored in canister state:

**SNS Governance** (`rs/sns/governance/canister/canister.rs`):
```rust
#[update]
fn reset_timers(_request: ResetTimersRequest) -> ResetTimersResponse {
    let reset_timers_cool_down_interval_seconds = RESET_TIMERS_COOL_DOWN_INTERVAL.as_secs();
    if let Some(timers) = governance_mut().proto.timers
        && let Some(last_reset_timestamp_seconds) = timers.last_reset_timestamp_seconds
    {
        assert!(
            now_seconds().saturating_sub(last_reset_timestamp_seconds)
                >= reset_timers_cool_down_interval_seconds, ...
        );
    }
    init_timers();
    ResetTimersResponse {}
}
```

The same pattern appears verbatim in SNS Root and SNS Swap. [1](#0-0) 

`init_timers()` unconditionally overwrites `last_reset_timestamp_seconds` with `now_seconds()`, resetting the global cooldown for every caller simultaneously: [2](#0-1) 

The same pattern in SNS Root: [3](#0-2) 

And SNS Swap: [4](#0-3) 

The cooldown intervals confirmed by integration tests are:
- SNS Governance: **600 seconds** (10 minutes)
- SNS Swap: **600 seconds** (10 minutes)
- SNS Root: **`ONE_WEEK_SECONDS`** (7 days) [5](#0-4) [6](#0-5) [7](#0-6) 

The `reset_timers` method is a recovery mechanism intended to restart stuck timers (e.g., after a canister upgrade). Because the cooldown is global and not per-caller, any unprivileged principal can call `reset_timers` at any time (subject only to the cooldown), which:
1. Resets the global `last_reset_timestamp_seconds` to `now`.
2. Blocks every other caller from calling `reset_timers` for the full cooldown period.

---

### Impact Explanation

When timers genuinely get stuck and a legitimate operator or community member needs to invoke `reset_timers`, an attacker who called `reset_timers` just before (when timers were healthy) has already consumed the cooldown window. The legitimate recovery call is rejected with `"Reset has already been called within the past N seconds"` and must wait for the full cooldown to expire. [8](#0-7) 

For SNS Root, the cooldown is one week. During that window, `run_periodic_tasks` (which polls for new archive canisters) cannot be restarted by any external actor. For SNS Governance and Swap, the window is 10 minutes, which is less severe but still exploitable to delay recovery in time-sensitive situations (e.g., during an active SNS swap finalization).

The attacker can sustain the grief indefinitely by calling `reset_timers` once per cooldown period, permanently preventing legitimate timer recovery without any privileged access.

---

### Likelihood Explanation

The entry path requires only a valid ingress message to a publicly reachable update method — no tokens, no neuron, no governance majority. Any principal on the IC can send this call. The cost is a single update call per cooldown period (trivially cheap in cycles). The attacker does not need to predict when timers will get stuck; they can preemptively call `reset_timers` at the start of every cooldown window as a standing grief.

---

### Recommendation

Add a caller identity check to `reset_timers` in all three canisters, restricting it to a trusted set of principals (e.g., the SNS governance canister itself, the NNS root, or a designated set of controllers). Alternatively, if the function must remain permissionless, track the cooldown per-caller using a `BTreeMap<PrincipalId, u64>` for `last_reset_timestamp_seconds`, so that one caller's invocation does not consume the global cooldown for all others — directly mirroring the fix recommended in H-01.

---

### Proof of Concept

1. SNS Root timers become stuck (e.g., after an upgrade).
2. Attacker sends an ingress `reset_timers` call to SNS Root at time `T`. This succeeds and sets `last_reset_timestamp_seconds = T`.
3. At time `T + 1`, a legitimate operator sends `reset_timers` to restart the stuck timers. The call is rejected: `"Reset has already been called within the past 604800 seconds"`.
4. The attacker repeats step 2 at time `T + ONE_WEEK_SECONDS`, perpetually blocking recovery.

The existing test `run_canister_reset_timers_cannot_be_spammed_test` already demonstrates that a second caller is blocked after the first succeeds — it just does not test the cross-principal case where the first and second callers are different principals: [9](#0-8)

### Citations

**File:** rs/sns/governance/canister/canister.rs (L626-642)
```rust
fn init_timers() {
    governance_mut().proto.timers.replace(Timers {
        last_reset_timestamp_seconds: Some(now_seconds()),
        ..Default::default()
    });

    let new_timer_id = ic_cdk_timers::set_timer_interval(RUN_PERIODIC_TASKS_INTERVAL, async || {
        run_periodic_tasks().await
    });
    TIMER_ID.with(|saved_timer_id| {
        let mut saved_timer_id = saved_timer_id.borrow_mut();
        if let Some(saved_timer_id) = *saved_timer_id {
            ic_cdk_timers::clear_timer(saved_timer_id);
        }
        saved_timer_id.replace(new_timer_id);
    });
}
```

**File:** rs/sns/governance/canister/canister.rs (L644-661)
```rust
#[update]
fn reset_timers(_request: ResetTimersRequest) -> ResetTimersResponse {
    let reset_timers_cool_down_interval_seconds = RESET_TIMERS_COOL_DOWN_INTERVAL.as_secs();

    if let Some(timers) = governance_mut().proto.timers
        && let Some(last_reset_timestamp_seconds) = timers.last_reset_timestamp_seconds
    {
        assert!(
            now_seconds().saturating_sub(last_reset_timestamp_seconds)
                >= reset_timers_cool_down_interval_seconds,
            "Reset has already been called within the past {reset_timers_cool_down_interval_seconds:?} seconds"
        );
    }

    init_timers();

    ResetTimersResponse {}
}
```

**File:** rs/sns/root/canister/canister.rs (L487-506)
```rust
#[update]
fn reset_timers(_request: ResetTimersRequest) -> ResetTimersResponse {
    let reset_timers_cool_down_interval_seconds = RESET_TIMERS_COOL_DOWN_INTERVAL.as_secs();

    STATE.with(|state| {
        let state = state.borrow();
        if let Some(timers) = state.timers
            && let Some(last_reset_timestamp_seconds) = timers.last_reset_timestamp_seconds {
                assert!(
                    now_seconds().saturating_sub(last_reset_timestamp_seconds)
                        >= reset_timers_cool_down_interval_seconds,
                    "Reset has already been called within the past {reset_timers_cool_down_interval_seconds:?} seconds"
                );
            }
    });

    init_timers();

    ResetTimersResponse {}
}
```

**File:** rs/sns/swap/canister/canister.rs (L348-365)
```rust
#[update]
fn reset_timers(_request: ResetTimersRequest) -> ResetTimersResponse {
    let reset_timers_cool_down_interval_seconds = RESET_TIMERS_COOL_DOWN_INTERVAL.as_secs();

    if let Some(timers) = swap_mut().timers
        && let Some(last_reset_timestamp_seconds) = timers.last_reset_timestamp_seconds
        && now_seconds().saturating_sub(last_reset_timestamp_seconds)
            < reset_timers_cool_down_interval_seconds
    {
        panic!(
            "Reset has already been called within the past {reset_timers_cool_down_interval_seconds:?} seconds"
        );
    }

    init_timers();

    ResetTimersResponse {}
}
```

**File:** rs/sns/integration_tests/src/timers.rs (L197-260)
```rust
fn run_canister_reset_timers_cannot_be_spammed_test(
    state_machine: &StateMachine,
    canister_id: CanisterId,
    reset_timers_cool_down_interval_seconds: u64,
) {
    // Ensure there was more than `reset_timers_cool_down_interval_seconds` seconds since the timers
    // were initialized.
    state_machine.advance_time(Duration::from_secs(reset_timers_cool_down_interval_seconds));
    state_machine.tick();

    let get_last_spawned_timestamp_seconds = || {
        let timers = get_timers(state_machine, canister_id);
        let last_reset_timestamp_seconds = assert_matches!(timers, Some(Timers {
            last_reset_timestamp_seconds: Some(last_reset_timestamp_seconds),
            ..
        }) => last_reset_timestamp_seconds);
        last_reset_timestamp_seconds
    };

    try_reset_timers(state_machine, canister_id).unwrap_or_else(|err| {
        panic!("Unable to call reset_timers on canister {canister_id:?}: {err}")
    });

    let last_spawned_timestamp_seconds_1 = get_last_spawned_timestamp_seconds();

    // Attempt to reset the timers again, after a small delay.
    let insufficient_for_resetting_timers_by_seconds = reset_timers_cool_down_interval_seconds
        .checked_sub(100)
        .unwrap();
    state_machine.advance_time(Duration::from_secs(
        insufficient_for_resetting_timers_by_seconds,
    ));
    state_machine.tick();

    {
        let err_text = try_reset_timers(state_machine, canister_id).unwrap_err();
        assert!(err_text.contains(&format!(
            "Reset has already been called within the past {reset_timers_cool_down_interval_seconds} seconds"
        )));
    }

    let last_spawned_timestamp_seconds_2 = get_last_spawned_timestamp_seconds();

    // The last call should not have had an effect.
    assert_eq!(
        last_spawned_timestamp_seconds_1,
        last_spawned_timestamp_seconds_2
    );

    // Attempt to reset the timers again after reset cool down.
    state_machine.advance_time(Duration::from_secs(100));
    state_machine.tick();

    try_reset_timers(state_machine, canister_id).unwrap_or_else(|err| {
        panic!("Unable to call reset_timers on canister {canister_id:?}: {err}")
    });

    let last_spawned_timestamp_seconds_3 = get_last_spawned_timestamp_seconds();

    assert_eq!(
        last_spawned_timestamp_seconds_3,
        last_spawned_timestamp_seconds_2 + reset_timers_cool_down_interval_seconds
    );
}
```

**File:** rs/sns/integration_tests/src/timers.rs (L346-346)
```rust
    run_canister_reset_timers_test(&state_machine, canister_id, 600, 60);
```

**File:** rs/sns/integration_tests/src/timers.rs (L362-362)
```rust
    run_canister_reset_timers_test(&state_machine, canister_id, 600, 60);
```

**File:** rs/sns/integration_tests/src/timers.rs (L428-428)
```rust
    run_canister_reset_timers_cannot_be_spammed_test(&state_machine, canister_id, ONE_WEEK_SECONDS);
```
