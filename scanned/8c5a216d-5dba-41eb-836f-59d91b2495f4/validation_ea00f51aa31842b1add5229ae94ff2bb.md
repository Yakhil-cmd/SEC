### Title
Lack of Access Control in `reset_timers()` Allows Any Caller to Disrupt SNS Canister Timer Operations - (File: rs/sns/governance/canister/canister.rs, rs/sns/root/canister/canister.rs, rs/sns/swap/canister/canister.rs)

---

### Summary

The `reset_timers` update endpoint is exposed publicly in all three core SNS canisters (governance, root, swap) without any caller authorization check. Any unprivileged ingress sender can invoke it, clearing and re-initializing the canister's internal timers. The only protection is a cooldown interval, which limits frequency but does not restrict who can call the function.

---

### Finding Description

In `rs/sns/governance/canister/canister.rs`, the `reset_timers` function is declared as a public `#[update]` endpoint:

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

There is no `msg_caller()` check, no controller check, and no governance-principal check anywhere in this function. The identical pattern exists in `rs/sns/root/canister/canister.rs` and `rs/sns/swap/canister/canister.rs`. A grep for `msg_caller`, `check_caller`, or `caller()` in all three files returns no results within the `reset_timers` function body.

The `RESET_TIMERS_COOL_DOWN_INTERVAL` for the swap canister is defined as 600 seconds (10 minutes). The governance and root canisters use the same constant name with analogous values. This cooldown is the sole guard — it throttles the rate of resets but does not restrict the caller identity at all.

Calling `reset_timers` invokes `init_timers()`, which clears the existing timer state and re-schedules all periodic tasks from scratch. For SNS governance, these timers drive reward distribution, proposal processing, and neuron maturity updates. For SNS swap, they drive auto-finalization. For SNS root, they drive periodic maintenance.

---

### Impact Explanation

**Impact: Medium-High**

An unprivileged attacker can call `reset_timers` on any SNS governance, root, or swap canister once every cooldown interval (e.g., every 10 minutes for swap). Each call clears the running timers and re-initializes them, causing:

- Delayed or disrupted periodic governance tasks (reward distribution, proposal tallying, neuron maturity updates) in SNS governance.
- Disrupted auto-finalization logic in SNS swap, potentially delaying token distribution to participants.
- Continuous, low-cost griefing of any deployed SNS instance by any anonymous user.

The attacker does not need any tokens, neurons, or privileged access — a plain ingress update call suffices.

---

### Likelihood Explanation

**Likelihood: High**

The endpoint is part of the public Candid interface of every deployed SNS canister. Any user who can send an ingress message to the IC (i.e., anyone) can call it. The attack requires no special setup, no funds, and no social engineering. The cooldown only limits the rate, not the actor.

---

### Recommendation

Add a caller authorization check at the top of each `reset_timers` function, restricting calls to the canister's own controllers or a designated privileged principal (e.g., the SNS governance canister for root/swap, or the NNS governance canister for SNS governance):

```rust
#[update]
fn reset_timers(_request: ResetTimersRequest) -> ResetTimersResponse {
    // Only controllers may reset timers.
    let caller = ic_cdk::api::msg_caller();
    assert!(
        ic_cdk::api::is_controller(&caller),
        "Caller is not authorized to reset timers"
    );
    // ... existing cooldown check and init_timers() call
}
```

Apply the same fix to `rs/sns/root/canister/canister.rs` and `rs/sns/swap/canister/canister.rs`.

---

### Proof of Concept

1. Identify any deployed SNS instance on the IC mainnet and obtain its governance canister ID.
2. Send an ingress update call to `reset_timers` with an empty `ResetTimersRequest` from any anonymous or user principal.
3. Observe that the call succeeds (returns `ResetTimersResponse {}`), clearing and re-initializing the governance timers.
4. Wait for the cooldown interval, then repeat — indefinitely disrupting periodic governance operations.

No privileged access, tokens, or neurons are required.

---

**Affected files:**

- `rs/sns/governance/canister/canister.rs` lines 644–661 [1](#0-0) 
- `rs/sns/root/canister/canister.rs` lines 487–506 [2](#0-1) 
- `rs/sns/swap/canister/canister.rs` (same pattern, `RESET_TIMERS_COOL_DOWN_INTERVAL` = 600 s) [3](#0-2)

### Citations

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

**File:** rs/sns/swap/canister/canister.rs (L46-49)
```rust
const RUN_PERIODIC_TASKS_INTERVAL: Duration = Duration::from_secs(60);

/// This guarantees that timers cannot be restarted more often than once every 10 intervals.
const RESET_TIMERS_COOL_DOWN_INTERVAL: Duration = Duration::from_secs(600);
```
