### Title
Unprivileged Caller Can Trigger Canister Trap via `NaiveDate::MIN` in `metrics_by_subnet` — (`rs/node_rewards/canister/src/metrics.rs`)

---

### Summary

An unprivileged caller can craft a `GetNodeProvidersRewardsRequest` with a date that resolves to `NaiveDate::MIN` (chrono year −262143), bypassing all guards in `validate_reward_period` and causing an unconditional `.unwrap()` panic in `metrics_by_subnet`, which traps the canister message.

---

### Finding Description

**Root cause — u32-to-i32 wrapping cast in `TryFrom<DateUtc> for NaiveDate`:**

`DateUtc.year` is typed as `Option<u32>`, but the conversion to `NaiveDate` casts it directly to `i32` with no bounds check: [1](#0-0) 

```rust
NaiveDate::from_ymd_opt(
    value.year.expect("Year is missing") as i32,  // ← wrapping cast
    ...
)
```

Passing `year = 4294705153u32` wraps to `−262143i32` via Rust's `as` truncation semantics (`u32::MAX − 262143 + 1 = 4294705153`). `NaiveDate::from_ymd_opt(−262143, 1, 1)` returns `Some(NaiveDate::MIN)` — a valid date — so `TryFrom` succeeds.

**Guard bypass — `validate_reward_period` has no lower-bound floor:** [2](#0-1) 

All three checks pass for `NaiveDate::MIN`:
- `last_day_synced >= NaiveDate::MIN` → always true
- `from_date <= to_date` → true (equal)
- `to_date < today` → true (year −262143 < any modern date)

**Panic site — `date.pred_opt().unwrap()` in `metrics_by_subnet`:** [3](#0-2) 

```rust
let first_key = SubnetMetricsKey {
    timestamp_nanos: first_unix_timestamp_nanoseconds(&date.pred_opt().unwrap()),
    //                                                        ^^^^^^^^^^^ None for NaiveDate::MIN
```

`NaiveDate::MIN.pred_opt()` returns `None`. `.unwrap()` panics unconditionally. This is reached via:

`get_node_providers_rewards` → loop body → `calculate_rewards_for_date` → `get_daily_metrics_by_subnet` → `metrics_manager.metrics_by_subnet(date)` [4](#0-3) 

---

### Impact Explanation

The panic causes a canister trap on the in-flight update message. The canister itself is not permanently crashed (IC rolls back state and rejects the message), but:
- Every call to `get_node_providers_rewards` or `get_node_providers_rewards_calculation` with this crafted date traps instead of returning a graceful `Err`.
- Any caller (no privileges required) can reliably trigger this.
- The question's concern about the timer recovery loop is not directly applicable (timers run independently), but repeated trapping of update calls is a confirmed DoS on those endpoints.

---

### Likelihood Explanation

The exploit requires only a single ingress update call with a crafted `u32` year value. No privileged access, no key material, no governance majority. The wrapping arithmetic is deterministic and reproducible.

---

### Recommendation

1. **Add a lower-bound floor in `validate_reward_period`** — reject any date before a reasonable epoch (e.g., 2020-01-01).
2. **Fix the `TryFrom<DateUtc> for NaiveDate` cast** — validate that `year as u32` fits in a positive `i32` range before casting, or use `i32::try_from(value.year...)` and reject on overflow.
3. **Replace `.unwrap()` in `metrics_by_subnet`** — use `.ok_or(...)` and propagate the error instead of panicking.

---

### Proof of Concept

```rust
// year = 4294705153u32 as i32 == -262143 == NaiveDate::MIN year
let request = GetNodeProvidersRewardsRequest {
    from_day: DateUtc { year: Some(4294705153), month: Some(1), day: Some(1) },
    to_day:   DateUtc { year: Some(4294705153), month: Some(1), day: Some(1) },
    algorithm_version: None,
};
// Expected: Err("...") — Actual: canister trap (panic at pred_opt().unwrap())
```

> **Note on the question's stated PoC:** `DateUtc { year: Some(1), ... }` (year 1 CE) does **not** trigger the panic — `NaiveDate::from_ymd_opt(1, 1, 1).pred_opt()` returns `Some(Dec 31, year 0)`. The actual exploit requires `year = 4294705153` to reach `NaiveDate::MIN` via the wrapping `as i32` cast.

### Citations

**File:** rs/node_rewards/canister/api/src/lib.rs (L60-70)
```rust
impl TryFrom<DateUtc> for NaiveDate {
    type Error = String;

    fn try_from(value: DateUtc) -> Result<Self, Self::Error> {
        NaiveDate::from_ymd_opt(
            value.year.expect("Year is missing") as i32,
            value.month.expect("Month is missing"),
            value.day.expect("Day is missing"),
        )
        .ok_or(format!("Invalid date: {:?}", value))
    }
```

**File:** rs/node_rewards/canister/src/canister/mod.rs (L153-179)
```rust
    fn validate_reward_period(
        &self,
        from_date: NaiveDate,
        to_date: NaiveDate,
    ) -> Result<(), String> {
        let last_day_synced = self
            .get_last_day_synced()
            .ok_or("Metrics and registry are not synced up")?;

        if last_day_synced < to_date {
            return Err("Metrics and registry are not synced up to to_date".to_string());
        }

        if from_date > to_date {
            return Err("from_date must be before to_date".to_string());
        }

        let today =
            DateTime::from_timestamp_nanos(current_time().as_nanos_since_unix_epoch() as i64)
                .date_naive();

        if to_date >= today {
            return Err("to_date must be earlier than today".to_string());
        }

        Ok(())
    }
```

**File:** rs/node_rewards/canister/src/canister/mod.rs (L217-228)
```rust
    fn get_daily_metrics_by_subnet(
        &self,
        date: &NaiveDate,
    ) -> Result<BTreeMap<SubnetId, Vec<NodeMetricsDailyRaw>>, String> {
        let metrics = self.metrics_manager.metrics_by_subnet(date);
        if metrics.is_empty() {
            return Err(format!(
                "No metrics found for day {}",
                date.format("%Y-%m-%d")
            ));
        }
        Ok(metrics)
```

**File:** rs/node_rewards/canister/src/metrics.rs (L183-186)
```rust
        let first_key = SubnetMetricsKey {
            timestamp_nanos: first_unix_timestamp_nanoseconds(&date.pred_opt().unwrap()),
            ..SubnetMetricsKey::min_key()
        };
```
