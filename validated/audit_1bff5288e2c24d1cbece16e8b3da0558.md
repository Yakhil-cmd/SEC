### Title
Unauthorized `reset_timers` Allows Any Caller to Manipulate SNS Governance Periodic Task Scheduling — (File: `rs/sns/governance/canister/canister.rs`)

---

### Summary
The `reset_timers` update endpoint in the SNS Governance canister has no caller authorization check. Any unprivileged ingress sender can call it to cancel and reschedule the timer that drives all periodic financial operations — reward distribution, maturity modulation updates, maturity disbursement finalization, and staked-maturity movement — delaying or disrupting those operations at will.

---

### Finding Description

`reset_timers` is a production `#[update]` method with no `check_caller` guard of any kind: [1](#0-0) 

The only protection present is a cooldown assertion, but it is wrapped in a conditional that fires only when **both** `timers` and `last_reset_timestamp_seconds` are `Some`. On a fresh deployment or immediately after an upgrade (before the first timer tick sets `last_reset_timestamp_seconds`), neither field is populated, so the cooldown is silently skipped and the function proceeds unconditionally to `init_timers()`.

`init_timers()` cancels any existing timer and schedules a fresh `run_periodic_tasks` interval: [2](#0-1) 

`run_periodic_tasks` is the single entry point for all time-sensitive financial state transitions in SNS Governance: [3](#0-2) 

These include:
- `distribute_rewards` — mints voting rewards to neuron holders
- `update_maturity_modulation` — updates the basis-point rate applied to all maturity disbursements
- `maybe_finalize_disburse_maturity` — executes pending ledger transfers for maturity disbursements
- `maybe_move_staked_maturity` — converts staked maturity into stake

The same pattern exists in SNS Root: [4](#0-3) 

By contrast, every other privileged state-mutation endpoint in the NNS/SNS stack enforces a caller check before touching state: [5](#0-4) 

---

### Impact Explanation

An unprivileged ingress sender can:

1. **Delay reward distribution** — by resetting the timer just before a reward round is due, the attacker pushes the round into the future, reducing the voting-power-weighted rewards that would have accrued.
2. **Freeze maturity modulation updates** — `update_maturity_modulation` is gated on `should_update_maturity_modulation` which checks elapsed time since last update. Continuously resetting the timer prevents the modulation value from refreshing, locking it at a stale rate that benefits or harms disbursing neurons.
3. **Delay maturity disbursement finalization** — pending ledger transfers (minting ICP to neuron holders) are only executed inside `run_periodic_tasks`; delaying the task delays actual token delivery.

The vulnerability class is **governance authorization bug** with direct financial impact on SNS token holders.

---

### Likelihood Explanation

- The endpoint is a standard Candid `update` method, reachable by any IC user with a valid identity.
- No cycles cost beyond the standard ingress fee is required.
- The cooldown bypass on fresh/upgraded canisters makes the first call unconditionally free of rate-limiting.
- Even with the cooldown active, a single call per cooldown window is sufficient to continuously push the timer forward and prevent periodic tasks from ever executing on schedule.

---

### Recommendation

Add a caller authorization check at the top of `reset_timers` (and the equivalent in SNS Root) restricting it to the canister itself or its controllers, mirroring the pattern used throughout the NNS/SNS stack:

```rust
#[update]
fn reset_timers(_request: ResetTimersRequest) -> ResetTimersResponse {
    // Only the canister itself or a controller may reset timers.
    let caller = ic_cdk::api::msg_caller();
    assert!(
        caller == ic_cdk::api::canister_self()
            || ic_cdk::api::is_controller(&caller),
        "Caller {} is not authorized to call reset_timers",
        caller
    );
    // ... existing cooldown check and init_timers() call
}
```

---

### Proof of Concept

```
# Any principal can call this with no special privileges:
dfx canister call <sns-governance-canister-id> reset_timers '(record {})'
# Returns: (record {})  -- succeeds unconditionally on a fresh canister

# Repeat every <RESET_TIMERS_COOL_DOWN_INTERVAL> seconds to permanently
# prevent run_periodic_tasks from executing on schedule, blocking reward
# distribution and maturity disbursements for all SNS neuron holders.
```

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

**File:** rs/sns/governance/src/governance.rs (L5471-5534)
```rust
    /// Runs periodic tasks that are not directly triggered by user input.
    pub async fn run_periodic_tasks(&mut self) {
        use ic_cdk::println;

        self.process_proposals();

        // None of the upgrade-related tasks should interleave with one another or themselves, so we acquire a global
        // lock for the duration of their execution. This will return `false` if the lock has already been acquired less
        // than 10 minutes ago by a previous invocation of `run_periodic_tasks`, in which case we skip the
        // upgrade-related tasks.
        if self.acquire_upgrade_periodic_task_lock() {
            // We only want to check the upgrade status if we are currently executing an upgrade.
            if self.should_check_upgrade_status() {
                self.check_upgrade_status().await;
            }

            if self.should_refresh_cached_upgrade_steps() {
                match self.try_temporarily_lock_refresh_cached_upgrade_steps() {
                    Err(err) => {
                        log!(ERROR, "{}", err);
                    }
                    Ok(deployed_version) => {
                        self.refresh_cached_upgrade_steps(deployed_version).await;
                    }
                }
            }

            self.initiate_upgrade_if_sns_behind_target_version().await;

            self.release_upgrade_periodic_task_lock();
        }

        let should_distribute_rewards = self.should_distribute_rewards();

        // Getting the total governance token supply from the ledger is expensive enough
        // that we don't want to do it on every call to `run_periodic_tasks`. So
        // we only fetch it when it's needed, which is when rewards should be
        // distributed
        if should_distribute_rewards {
            match self.ledger.total_supply().await {
                Ok(supply) => {
                    // Distribute rewards
                    self.distribute_rewards(supply);
                }
                Err(e) => log!(
                    ERROR,
                    "Error when getting total governance token supply: {}",
                    GovernanceError::from(e)
                ),
            }
        }

        if self.should_update_maturity_modulation() {
            self.update_maturity_modulation().await;
        }

        self.maybe_finalize_disburse_maturity().await;

        self.maybe_move_staked_maturity();

        self.compute_cached_metrics().await;

        self.maybe_gc();
    }
```

**File:** rs/sns/root/canister/canister.rs (L487-510)
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

/// Encode the metrics in a format that can be understood by Prometheus.
fn encode_metrics(_w: &mut ic_metrics_encoder::MetricsEncoder<Vec<u8>>) -> std::io::Result<()> {
    Ok(())
```

**File:** rs/nns/common/src/access_control.rs (L7-11)
```rust
pub fn check_caller_is_root() {
    if caller() != PrincipalId::from(ic_nns_constants::ROOT_CANISTER_ID) {
        panic!("Only the root canister is allowed to call this method.");
    }
}
```
