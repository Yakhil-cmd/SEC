The code is fully readable. Here is the analysis:

---

### Title
Unprivileged Ingress Caller Can Invoke `reset_timers` on SNS Root, Delaying Archive Polling by Up to 24 Hours — (`rs/sns/root/canister/canister.rs`)

### Summary

`reset_timers` is a public `#[update]` method on the SNS root canister with **no caller authorization check**. Any anonymous or unprivileged principal can call it once per 7-day cooldown window. Each successful call clears the existing periodic timer and schedules a fresh 24-hour interval, delaying the next `run_periodic_tasks` execution (which polls for new archive canisters) by up to 24 hours.

### Finding Description

`reset_timers` at [1](#0-0)  carries only the `#[update]` attribute. There is no `ic_cdk::api::caller()` check, no governance-only guard, and no allowlist. The sole protection is the 7-day cooldown:

```rust
assert!(
    now_seconds().saturating_sub(last_reset_timestamp_seconds)
        >= reset_timers_cool_down_interval_seconds,
    ...
);
``` [2](#0-1) 

When the cooldown has elapsed, `init_timers()` is called unconditionally: [3](#0-2) 

`init_timers` clears the existing `TIMER_ID` and registers a new `set_timer_interval` with `RUN_PERIODIC_TASKS_INTERVAL` (24 hours). The Candid interface confirms `reset_timers` is publicly exposed with no restrictions: [4](#0-3) 

The periodic task that fires on this timer is `run_periodic_tasks`, which calls `SnsRootCanister::poll_for_new_archive_canisters`: [5](#0-4) 

### Impact Explanation

An attacker who calls `reset_timers` immediately after the cooldown elapses resets the 24-hour interval from zero. If the timer was about to fire (e.g., 1 second away), the next archive poll is delayed by up to **24 hours** (one full `RUN_PERIODIC_TASKS_INTERVAL`). This means `archive_canister_ids` in `SnsRootCanister` state can remain stale for an extra 24 hours, and `get_sns_canisters_summary` may omit newly spawned archive canisters during that window.

**Correction to the question's impact claim:** The maximum delay per reset is **24 hours** (one `RUN_PERIODIC_TASKS_INTERVAL`), not 7 days. The 7-day cooldown limits attack frequency to once per week, not the delay magnitude. [6](#0-5) 

### Likelihood Explanation

The attack path is trivially reachable: any anonymous principal submits an ingress update call to `reset_timers` after 7 days have elapsed since the last reset. No privileged access, no key material, and no coordination is required. The Candid interface is public.

### Recommendation

Add a caller authorization check to `reset_timers`. Only the SNS governance canister (or a defined allowlist) should be permitted to invoke it, consistent with how other sensitive methods like `manage_dapp_canister_settings` are guarded: [7](#0-6) 

### Proof of Concept

State-machine test outline:
1. Install SNS root canister.
2. Advance time by `ONE_WEEK_SECONDS` so the cooldown elapses and the first periodic task fires.
3. Advance time to just before the next 24-hour tick (e.g., 23h 59m 59s after last spawn).
4. Call `reset_timers` as `Principal::anonymous()` — succeeds with no error.
5. Advance time by `ONE_DAY_SECONDS` and tick.
6. Assert `last_spawned_timestamp_seconds` is now `~24h` after the reset call, not `~1 second` after step 3 — confirming the poll was delayed by a full interval.

The existing test infrastructure in `rs/sns/integration_tests/src/timers.rs` already demonstrates this pattern using `execute_ingress` with no caller restriction: [8](#0-7)

### Citations

**File:** rs/sns/root/canister/canister.rs (L47-50)
```rust
const RUN_PERIODIC_TASKS_INTERVAL: Duration = Duration::from_secs(60 * 60 * 24); // one day

/// This guarantees that timers cannot be restarted more often than once every 7 intervals.
const RESET_TIMERS_COOL_DOWN_INTERVAL: Duration = Duration::from_secs(60 * 60 * 24 * 7); // one week
```

**File:** rs/sns/root/canister/canister.rs (L419-427)
```rust
fn assert_eq_governance_canister_id(id: PrincipalId) {
    STATE.with(|state: &RefCell<SnsRootCanister>| {
        let state = state.borrow();
        let governance_canister_id = state
            .governance_canister_id
            .expect("STATE.governance_canister_id is not populated");
        assert_eq!(id, governance_canister_id);
    });
}
```

**File:** rs/sns/root/canister/canister.rs (L447-457)
```rust
async fn run_periodic_tasks() {
    STATE.with(|state| {
        let mut state = state.borrow_mut();
        if let Some(ref mut timers) = state.timers {
            timers.last_spawned_timestamp_seconds.replace(now_seconds());
        };
    });

    let ledger_client = create_ledger_client();
    SnsRootCanister::poll_for_new_archive_canisters(&STATE, &ledger_client).await
}
```

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
