### Title
Unprivileged Griefing via Unauthenticated `reset_timers` Disrupts SNS Periodic Task Execution - (File: rs/sns/governance/canister/canister.rs, rs/sns/swap/canister/canister.rs, rs/sns/root/canister/canister.rs)

---

### Summary

The `reset_timers` update method on the SNS Governance, SNS Swap, and SNS Root canisters carries no caller-identity check. Any unprivileged ingress sender can invoke it once per cooldown window (600 s for Governance/Swap, one week for Root). Each successful call cancels the live periodic-task timer and schedules a fresh one, introducing a forced delay before the next periodic-task execution. An attacker who calls the endpoint at the maximum allowed rate can continuously push back periodic tasks — including swap auto-finalization, maturity disbursement, and proposal processing — for the entire lifetime of the canister.

---

### Finding Description

`reset_timers` is declared as a plain `#[update]` method with no `ic_cdk::api::caller()` guard in all three canisters:

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

`RESET_TIMERS_COOL_DOWN_INTERVAL` is 600 seconds for Governance and Swap, and `ONE_WEEK_SECONDS` for Root. [1](#0-0) [2](#0-1) 

`init_timers()` unconditionally cancels the running timer and registers a new interval:

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
            ic_cdk_timers::clear_timer(saved_timer_id);   // ← kills the live timer
        }
        saved_timer_id.replace(new_timer_id);
    });
}
``` [3](#0-2) [4](#0-3) 

The same pattern exists in SNS Root: [5](#0-4) 

The Candid interface exposes `reset_timers` as a public method with no restrictions: [6](#0-5) 

The parallel to MochiVault is exact:

| MochiVault | IC SNS |
|---|---|
| `deposit()` callable by anyone | `reset_timers()` callable by anyone |
| Resets `lastDeposit[_id]` to `block.timestamp` | Resets `last_reset_timestamp_seconds` to `now_seconds()` |
| Blocks withdrawal for `delay()` (3 min) | Delays next periodic task by up to `RUN_PERIODIC_TASKS_INTERVAL` (10 s governance, 60 s swap) |
| Attacker repeats every 3 min | Attacker repeats every 600 s |

---

### Impact Explanation

Every call to `reset_timers` kills the currently scheduled periodic-task timer and restarts it from zero. Periodic tasks in these canisters include:

- **SNS Governance**: proposal reward distribution, maturity disbursement finalization, neuron management housekeeping.
- **SNS Swap**: advancing the swap lifecycle, auto-finalization of the decentralization swap.

An attacker calling `reset_timers` at the maximum rate (once per 600 s) on the Swap canister (task interval = 60 s) reduces effective task throughput by ~10% and can delay time-sensitive swap finalization. For the Governance canister (task interval = 10 s) the per-reset delay is smaller but still continuous. Because the cooldown check only prevents calls *within* the window — it does not restrict *who* may call — the attacker can sustain this indefinitely at zero cost beyond transaction fees.

---

### Likelihood Explanation

The method is publicly listed in the Candid interface and requires no tokens, no neuron ownership, and no privileged principal. Any IC user can send an ingress update call to the canister. On low-fee subnets the cost per call is negligible. The attack is fully automatable: a script that calls `reset_timers` every 601 seconds will run indefinitely without any special access.

---

### Recommendation

Add a caller-identity guard to `reset_timers` in all three canisters. Only the SNS governance canister itself, the NNS root/governance canister, or an explicitly whitelisted set of principals should be permitted to invoke this recovery endpoint. Example pattern:

```rust
#[update]
fn reset_timers(_request: ResetTimersRequest) -> ResetTimersResponse {
    let caller = ic_cdk::api::caller();
    assert!(
        is_authorized_caller(&caller),
        "Caller {} is not authorized to reset timers", caller
    );
    // ... existing cooldown check ...
    init_timers();
    ResetTimersResponse {}
}
```

Alternatively, if the intent is to allow any caller as a liveness recovery mechanism, the cooldown window should be increased substantially (e.g., to 24 hours) to make sustained griefing economically unattractive.

---

### Proof of Concept

1. Obtain the canister ID of any deployed SNS Governance or SNS Swap canister.
2. Wait until `now - last_reset_timestamp_seconds >= 600`.
3. Send an ingress update call:
   ```
   dfx canister call <sns-governance-id> reset_timers '(record {})'
   ```
4. Observe that `last_reset_timestamp_seconds` is updated and the periodic-task timer is restarted from zero.
5. Repeat step 3 every 601 seconds from any principal. The periodic-task timer is continuously reset, delaying all governance/swap periodic operations for the lifetime of the canister.

The existing integration test `run_canister_reset_timers_cannot_be_spammed_test` confirms the cooldown fires correctly but also confirms that any caller (no identity check) can invoke the function once per window: [7](#0-6) [8](#0-7)

### Citations

**File:** rs/sns/governance/canister/canister.rs (L73-76)
```rust
/// This guarantees that timers cannot be restarted more often than once every 60 intervals.
const RESET_TIMERS_COOL_DOWN_INTERVAL: Duration = Duration::from_secs(600);

const RUN_PERIODIC_TASKS_INTERVAL: Duration = Duration::from_secs(10);
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

**File:** rs/sns/swap/canister/canister.rs (L46-49)
```rust
const RUN_PERIODIC_TASKS_INTERVAL: Duration = Duration::from_secs(60);

/// This guarantees that timers cannot be restarted more often than once every 10 intervals.
const RESET_TIMERS_COOL_DOWN_INTERVAL: Duration = Duration::from_secs(600);
```

**File:** rs/sns/swap/canister/canister.rs (L318-346)
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
