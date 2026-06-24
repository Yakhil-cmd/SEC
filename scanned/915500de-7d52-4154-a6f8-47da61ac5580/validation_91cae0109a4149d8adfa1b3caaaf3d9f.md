### Title
Missing Caller Authorization in `reset_timers` Allows Any Unprivileged User to Disrupt SNS Periodic Task Scheduling - (File: `rs/sns/governance/canister/canister.rs`)

---

### Summary

The `reset_timers` update endpoint in the SNS Governance, SNS Root, and SNS Swap canisters performs no caller authorization check. Any unprivileged ingress sender can invoke it to clear the running periodic-task timer and restart it, disrupting the scheduling of critical governance operations such as reward distribution and proposal execution. The only guard is a cool-down window, not an identity check.

---

### Finding Description

The `reset_timers` function is exposed as a public `#[update]` method in three production SNS canisters. In each case the implementation reads the cool-down timestamp and, if enough time has elapsed, calls `init_timers()` — which clears the existing `ic_cdk_timers` timer and registers a fresh interval — without ever inspecting `ic_cdk::api::msg_caller()`.

**SNS Governance canister** (`rs/sns/governance/canister/canister.rs`, lines 644–661):

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

    init_timers();   // ← clears existing timer, registers new interval, resets state
    ResetTimersResponse {}
}
``` [1](#0-0) 

`init_timers()` cancels the live timer, registers a new `set_timer_interval`, and resets `last_reset_timestamp_seconds` / `last_spawned_timestamp_seconds` in the canister's persistent `Timers` proto: [2](#0-1) 

The same pattern — `#[update]` with no caller check — is present in the SNS Root canister: [3](#0-2) 

and the SNS Swap canister: [4](#0-3) 

All three endpoints are listed in their respective public Candid interfaces with no access restriction annotation: [5](#0-4) [6](#0-5) 

Compare with other sensitive SNS Governance endpoints that correctly enforce caller identity — for example `set_mode`, which panics if the caller is not the swap canister: [7](#0-6) 

No equivalent guard exists for `reset_timers`.

---

### Impact Explanation

`init_timers()` performs two state-mutating actions:

1. **Cancels the live periodic-task timer** (`ic_cdk_timers::clear_timer`), delaying the next execution of `run_periodic_tasks` by up to one full `RUN_PERIODIC_TASKS_INTERVAL` (10 s for Governance, 60 s for Swap, 24 h for Root).
2. **Resets `last_spawned_timestamp_seconds` to `None`** in the persistent `Timers` proto, corrupting the observability record used to detect stuck timers. [8](#0-7) 

`run_periodic_tasks` drives reward distribution, proposal execution, neuron spawning, and upgrade-version checks. Repeated, attacker-triggered resets (once per cool-down window: every 600 s for Governance/Swap, once per week for Root) can cumulatively delay these operations. For the SNS Root canister, a single call delays the next periodic check by up to 24 hours, which can stall canister-status monitoring and upgrade tracking for an entire SNS instance.

---

### Likelihood Explanation

The endpoint is reachable by any Internet Computer principal via a standard ingress update call — no neuron, no token stake, no privileged role required. The Candid interface is public. The only friction is the cool-down window, which an attacker can trivially respect while still calling the function at maximum frequency. This is a low-effort, permissionless attack path.

---

### Recommendation

Add a caller authorization check at the top of each `reset_timers` handler, analogous to the pattern used elsewhere in the NNS/SNS codebase:

```rust
#[update]
fn reset_timers(_request: ResetTimersRequest) -> ResetTimersResponse {
    // Only controllers or a designated recovery principal may reset timers.
    let caller = ic_cdk::api::msg_caller();
    assert!(
        ic_cdk::api::is_controller(&caller),
        "Caller {} is not authorized to reset timers.", caller
    );
    // ... existing cool-down check and init_timers() call
}
```

Alternatively, restrict to the SNS Root canister (for Governance) or the SNS Governance canister (for Root/Swap), mirroring the inter-canister trust model already established for other privileged endpoints. [9](#0-8) 

---

### Proof of Concept

Any unprivileged principal can execute the following ingress call against a deployed SNS Governance canister:

```bash
dfx canister --network ic call <sns-governance-canister-id> reset_timers '(record {})'
```

This succeeds without any neuron ownership, token stake, or controller privilege. After the 600-second cool-down elapses, the call can be repeated indefinitely. Each successful call:

- Cancels the live `run_periodic_tasks` timer.
- Registers a fresh 10-second interval timer (resetting the countdown).
- Overwrites `last_reset_timestamp_seconds` and nullifies `last_spawned_timestamp_seconds` in the canister's stable `Timers` state.

The same call works against the SNS Root (`reset_timers_cool_down_interval_seconds = ONE_WEEK_SECONDS`) and SNS Swap (`600 s`) canisters. [10](#0-9) [11](#0-10)

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

**File:** rs/sns/root/canister/canister.rs (L49-50)
```rust
/// This guarantees that timers cannot be restarted more often than once every 7 intervals.
const RESET_TIMERS_COOL_DOWN_INTERVAL: Duration = Duration::from_secs(60 * 60 * 24 * 7); // one week
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

**File:** rs/sns/governance/canister/governance.did (L1071-1072)
```text
  reset_timers : (record {}) -> (record {});
  get_timers : (record {}) -> (GetTimersResponse) query;
```

**File:** rs/sns/root/canister/root.did (L242-243)
```text
  reset_timers : (record {}) -> (record {});
  get_timers : (record {}) -> (GetTimersResponse) query;
```

**File:** rs/sns/governance/src/governance.rs (L785-791)
```rust
    pub fn set_mode(&mut self, mode: i32, caller: PrincipalId) {
        let mode =
            governance::Mode::try_from(mode).unwrap_or_else(|_| panic!("Unknown mode: {mode}"));

        if !self.is_swap_canister(caller) {
            panic!("Caller must be the swap canister.");
        }
```

**File:** rs/nns/common/src/access_control.rs (L7-11)
```rust
pub fn check_caller_is_root() {
    if caller() != PrincipalId::from(ic_nns_constants::ROOT_CANISTER_ID) {
        panic!("Only the root canister is allowed to call this method.");
    }
}
```
