### Title
Unprivileged Caller Can Invoke `reset_timers` on SNS Root, Preempting Legitimate Resets and Blocking Timer Recovery for Up to 7 Days - (`rs/sns/root/canister/canister.rs`)

---

### Summary

The `reset_timers` update method on SNS Root has no caller authorization check. Any principal — including anonymous — can invoke it once the 7-day cooldown expires. An attacker can race to call it the moment the cooldown window opens, update `last_reset_timestamp_seconds` to an attacker-chosen time, and thereby block any legitimate operator reset for another full 7-day period.

---

### Finding Description

`reset_timers` is defined at line 488 with the `#[update]` macro: [1](#0-0) 

The function body performs exactly one guard — a cooldown check against `last_reset_timestamp_seconds`: [2](#0-1) 

There is **no** `ic_cdk::api::caller()` check anywhere in the function. Compare this with, for example, `get_sns_canisters_summary`, which explicitly gates the sensitive path on the governance canister ID: [3](#0-2) 

`reset_timers` has no equivalent gate. The cooldown constant is: [4](#0-3) 

When the cooldown expires, the first caller — privileged or not — wins the window. After a successful call, `init_timers()` is invoked, which updates `last_reset_timestamp_seconds` to `now_seconds()` and restarts the periodic interval timer: [5](#0-4) 

---

### Impact Explanation

The periodic timer drives `run_periodic_tasks`, which is responsible for archive canister discovery (polling the ledger for archive info). The `reset_timers` mechanism exists as a recovery path for when this timer becomes stuck.

An attacker who calls `reset_timers` at cooldown expiry:
1. Resets `last_reset_timestamp_seconds` to the current time, consuming the recovery window.
2. Prevents any legitimate operator or governance-initiated reset for the next 7 days.
3. Can repeat this every 7 days indefinitely.

If the periodic timer subsequently fails (e.g., due to a canister upgrade that clears timer state), the legitimate recovery path is blocked for up to 7 days per cycle, disrupting archive canister discovery during that window.

---

### Likelihood Explanation

The attack requires only:
- Knowing the SNS Root canister ID (public).
- Monitoring `last_reset_timestamp_seconds` via `get_timers` (a public query).
- Submitting an ingress `reset_timers` call at the right moment.

No privileged access, no key material, no social engineering. Fully automatable by a script watching the timer state.

---

### Recommendation

Add a caller authorization check at the top of `reset_timers`, restricting invocation to the SNS governance canister (and/or the NNS root), consistent with how other sensitive methods are guarded:

```rust
#[update]
fn reset_timers(_request: ResetTimersRequest) -> ResetTimersResponse {
    let caller = PrincipalId(ic_cdk::api::caller());
    assert_eq_governance_canister_id(caller); // or allow NNS root as well
    // ... existing cooldown check ...
}
```

---

### Proof of Concept

State-machine test outline:
1. Initialize SNS Root canister.
2. Advance time by `ONE_WEEK_SECONDS` (≥ `RESET_TIMERS_COOL_DOWN_INTERVAL`).
3. Call `reset_timers` as the **anonymous principal** — assert it succeeds and `last_reset_timestamp_seconds` is updated.
4. Immediately call `reset_timers` again as the anonymous principal — assert it **panics** (cooldown active).
5. Attempt `reset_timers` as the legitimate governance canister — assert it also **panics** (cooldown consumed by attacker).
6. Confirm the 7-day window is now locked out for legitimate callers.

The absence of any `caller()` check at lines 488–505 makes step 3 succeed unconditionally for any principal. [6](#0-5)

### Citations

**File:** rs/sns/root/canister/canister.rs (L49-50)
```rust
/// This guarantees that timers cannot be restarted more often than once every 7 intervals.
const RESET_TIMERS_COOL_DOWN_INTERVAL: Duration = Duration::from_secs(60 * 60 * 24 * 7); // one week
```

**File:** rs/sns/root/canister/canister.rs (L183-185)
```rust
    if update_canister_list {
        assert_eq_governance_canister_id(PrincipalId(ic_cdk::api::caller()));
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
