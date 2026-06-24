### Title
Insufficient Access Control on `reset_timers` Allows Any Caller to Continuously Disrupt SNS Periodic Task Execution - (File: rs/sns/governance/canister/canister.rs, rs/sns/swap/canister/canister.rs, rs/sns/root/canister/canister.rs)

---

### Summary

The `reset_timers` update method is publicly exposed in SNS Governance, SNS Swap, and SNS Root canisters without any caller authentication. Any unprivileged ingress sender can invoke it. While a time-based cooldown limits call frequency, an attacker can exploit this to continuously delay the execution of periodic tasks by resetting the timer interval on each cooldown expiry, analogous to the `AutoRedemption::performUpkeep` access-control gap in the external report.

---

### Finding Description

All three SNS canisters expose `reset_timers` as a public `#[update]` method with no `ic_cdk::api::msg_caller()` / `ic_cdk::api::is_controller()` check:

**SNS Governance** (`rs/sns/governance/canister/canister.rs`, lines 644–661):
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

**SNS Swap** (`rs/sns/swap/canister/canister.rs`, lines 348–365) and **SNS Root** (`rs/sns/root/canister/canister.rs`, lines 487–506) follow the identical pattern.

The only guard is a time-based cooldown (`RESET_TIMERS_COOL_DOWN_INTERVAL`): 600 seconds for SNS Governance and SNS Swap, and `ONE_WEEK_SECONDS` for SNS Root.

`init_timers()` performs two destructive operations:

1. **Clears the existing running timer** via `ic_cdk_timers::clear_timer(saved_timer_id)`.
2. **Schedules a fresh interval** starting from the current moment. [1](#0-0) [2](#0-1) [3](#0-2) 

If a periodic task was about to fire (e.g., 1 second away), calling `reset_timers` pushes it back by the full `RUN_PERIODIC_TASKS_INTERVAL`. An attacker who calls `reset_timers` at the moment the cooldown expires, every cooldown period, can continuously prevent the periodic task from ever firing — provided `RUN_PERIODIC_TASKS_INTERVAL ≥ RESET_TIMERS_COOL_DOWN_INTERVAL`. Even when the interval is shorter, the attacker can cause repeated, measurable delays to governance-critical operations.

The Candid interface confirms `reset_timers` is a publicly advertised service method with no access restriction: [4](#0-3) 

Integration tests confirm any caller can invoke it and that the only protection is the cooldown: [5](#0-4) 

---

### Impact Explanation

**SNS Swap**: Periodic tasks drive swap lifecycle transitions (e.g., finalizing a decentralization swap, distributing tokens). An attacker calling `reset_timers` every 600 seconds can delay these transitions indefinitely, preventing a swap from completing on time and potentially causing it to expire or leaving participants in limbo. This is a governance/financial impact on a live SNS launch.

**SNS Governance**: Periodic tasks include neuron reward distribution and proposal processing. Continuous timer resets delay reward payouts and governance actions for all SNS token holders.

**SNS Root**: Periodic tasks include canister health checks and archive management. A one-week cooldown limits the attack to once per week, but each call can delay the next task by up to `RUN_PERIODIC_TASKS_INTERVAL` (one day per integration test evidence). [6](#0-5) 

---

### Likelihood Explanation

The attack path requires only a standard ingress message from any principal — no privileged key, no governance majority, no threshold corruption. The attacker needs only to wait for the cooldown to expire and send a single update call. This is trivially automatable. The SNS canisters are deployed on mainnet and their Candid interfaces are publicly known.

---

### Recommendation

Add a caller authorization check at the top of each `reset_timers` implementation, restricting it to controllers or the NNS governance canister, consistent with the pattern already used elsewhere in the codebase:

```rust
#[update]
fn reset_timers(_request: ResetTimersRequest) -> ResetTimersResponse {
    // Add: only controllers or NNS governance may call this
    let caller = ic_cdk::api::msg_caller();
    assert!(
        ic_cdk::api::is_controller(&caller) || caller == GOVERNANCE_CANISTER_ID,
        "Caller is not authorized to reset timers"
    );
    // ... existing cooldown check and init_timers() ...
}
```

This mirrors the pattern used in `rs/migration_canister/src/privileged.rs`: [7](#0-6) 

---

### Proof of Concept

1. Identify a live SNS instance (Governance, Swap, or Root canister ID).
2. Query `get_timers` to observe `last_reset_timestamp_seconds`.
3. Wait until `now - last_reset_timestamp_seconds >= RESET_TIMERS_COOL_DOWN_INTERVAL` (600 s for Governance/Swap, 1 week for Root).
4. Send an ingress update call to `reset_timers` from any principal (no special identity required).
5. Observe via `get_timers` that `last_reset_timestamp_seconds` is updated to now, and the internal timer interval restarts from zero.
6. If the periodic task was scheduled to fire imminently, it is now delayed by the full `RUN_PERIODIC_TASKS_INTERVAL`.
7. Repeat step 3–6 every cooldown period to continuously defer periodic task execution. [8](#0-7) [9](#0-8)

### Citations

**File:** rs/sns/root/canister/canister.rs (L466-485)
```rust
fn init_timers() {
    STATE.with(|state| {
        let mut state = state.borrow_mut();
        state.timers.replace(Timers {
            last_reset_timestamp_seconds: Some(now_seconds()),
            ..Default::default()
        });
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

**File:** rs/sns/swap/canister/canister.rs (L318-345)
```rust
fn init_timers() {
    let last_reset_timestamp_seconds = Some(now_seconds());
    let requires_periodic_tasks = swap().requires_periodic_tasks();

    swap_mut().timers.replace(Timers {
        requires_periodic_tasks: Some(requires_periodic_tasks),
        last_reset_timestamp_seconds,
        ..Default::default()
    });

    if requires_periodic_tasks {
        let new_timer_id =
            ic_cdk_timers::set_timer_interval(RUN_PERIODIC_TASKS_INTERVAL, async || {
                run_periodic_tasks().await
            });
        TIMER_ID.with(|saved_timer_id| {
            let mut saved_timer_id = saved_timer_id.borrow_mut();
            if let Some(saved_timer_id) = *saved_timer_id {
                ic_cdk_timers::clear_timer(saved_timer_id);
            }
            saved_timer_id.replace(new_timer_id);
        });
    } else {
        log!(
            INFO,
            "Periodic tasks are not required for this Swap anymore."
        );
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

**File:** rs/sns/root/canister/root.did (L242-243)
```text
  reset_timers : (record {}) -> (record {});
  get_timers : (record {}) -> (GetTimersResponse) query;
```

**File:** rs/sns/integration_tests/src/timers.rs (L102-113)
```rust
fn try_reset_timers(state_machine: &StateMachine, canister_id: CanisterId) -> Result<(), String> {
    let payload = Encode!(&ResetTimersRequest {}).unwrap();
    let response = state_machine.execute_ingress(canister_id, "reset_timers", payload);
    match response {
        Ok(response) => {
            let response = response.bytes();
            let ResetTimersResponse {} = Decode!(&response, ResetTimersResponse).unwrap();
            Ok(())
        }
        Err(err) => Err(err.to_string()),
    }
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

**File:** rs/migration_canister/src/privileged.rs (L14-19)
```rust
fn check_caller() -> Result<(), Option<MigrationCanisterError>> {
    let is_controller = ic_cdk::api::is_controller(&msg_caller());
    match is_controller || (msg_caller() == Principal::from_text(GOVERNANCE_CANISTER_ID).unwrap()) {
        true => Ok(()),
        false => Err(Some(MigrationCanisterError::CallerNotAuthorized(Reserved))),
    }
```
