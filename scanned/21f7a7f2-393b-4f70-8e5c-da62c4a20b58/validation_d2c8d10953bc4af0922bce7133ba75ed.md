The exploit path is traceable through the code. Let me walk through each step precisely.

**Step 1: The unchecked cast in `TryFrom<DateUtc> for NaiveDate`** [1](#0-0) 

The cast `value.year.expect("Year is missing") as i32` is a **wrapping/truncating cast** in Rust. `u32::MAX as i32 = -1` (two's complement). There is no bounds check — any `u32` value above `i32::MAX` silently wraps to a negative year.

**Step 2: `NaiveDate::from_ymd_opt(-1, 1, 1)` succeeds**

Chrono supports years from -262144 to 262143. Year -1 is valid, so `from_ymd_opt(-1, 1, 1)` returns `Some(NaiveDate)`, and `TryFrom` returns `Ok(NaiveDate(-1, 1, 1))` — **no error is raised**.

**Step 3: `validate_reward_period` does not block year -1** [2](#0-1) 

With `to_date = NaiveDate(-1, 1, 1)` and `last_day_synced` being a recent date (e.g., 2024-01-01):
- `last_day_synced < to_date` → `2024 < -1` → **FALSE** — check does not fire
- `from_date > to_date` → same date → **FALSE**
- `to_date >= today` → `-1 >= 2024` → **FALSE**

All three guards pass. Execution continues.

**Step 4: `calculate_rewards_for_date` → `get_rewards_table` → `last_unix_timestamp_nanoseconds` panics** [3](#0-2) [4](#0-3) 

`timestamp_nanos_opt()` returns `None` for any date outside the i64-nanosecond-representable range (~1677–2262). Year -1 is far outside this range. The `.unwrap()` on line 18 **panics**, causing a canister trap.

---

**Access control question:** I was unable to read `main.rs` to confirm whether `get_node_providers_rewards` is exposed without caller restrictions. If it is a public update call (no `caller` guard), the path is fully reachable by an unprivileged principal. The governance canister calls it via inter-canister call, but the canister endpoint itself may be open.

---

### Title
Unchecked `u32→i32` Year Cast in `TryFrom<DateUtc>` Bypasses Validation and Causes `unwrap()` Panic in `last_unix_timestamp_nanoseconds` — (`rs/node_rewards/canister/src/chrono_utils.rs`, `rs/node_rewards/canister/api/src/lib.rs`)

### Summary
An attacker supplying `DateUtc { year: Some(u32::MAX), month: Some(1), day: Some(1) }` causes a silent wrapping cast to year `-1`, which passes all date validation, then panics at `.unwrap()` inside `last_unix_timestamp_nanoseconds` when `timestamp_nanos_opt()` returns `None`.

### Finding Description
`TryFrom<DateUtc> for NaiveDate` casts `year: u32` to `i32` with `as i32` — a wrapping cast in Rust. `u32::MAX as i32 = -1`. `NaiveDate::from_ymd_opt(-1, 1, 1)` succeeds (chrono supports BCE years), so no error is returned. The resulting `NaiveDate(-1, 1, 1)` passes all three guards in `validate_reward_period` (year -1 is numerically less than any recent `last_day_synced` and less than today). When `calculate_rewards_for_date` is called for this date, `get_rewards_table` calls `last_unix_timestamp_nanoseconds`, which calls `.timestamp_nanos_opt().unwrap()`. For year -1, `timestamp_nanos_opt()` returns `None`, and `.unwrap()` panics, trapping the canister.

### Impact Explanation
Canister trap on the node rewards canister for any call to `get_node_providers_rewards` with a crafted overflow year. In the IC, a trap rolls back state and rejects the message; the canister itself continues operating. Impact is a per-call denial of service / unexpected trap rather than persistent corruption or fund loss.

### Likelihood Explanation
The `DateUtc` type is a public Candid-facing API type with `year: Option<u32>`. Any caller who can invoke `get_node_providers_rewards` can supply `u32::MAX`. The bug is trivially reproducible with a unit test. Likelihood depends on whether the endpoint has a caller guard in `main.rs` (not verified).

### Recommendation
1. In `TryFrom<DateUtc> for NaiveDate`, replace the unchecked cast with a checked conversion:
   ```rust
   let year = i32::try_from(value.year.expect("Year is missing"))
       .map_err(|_| "Year out of valid i32 range".to_string())?;
   ```
2. Replace `.unwrap()` in `first_unix_timestamp_nanoseconds` and `last_unix_timestamp_nanoseconds` with `.ok_or(...)` and propagate the error, rather than panicking.

### Proof of Concept
```rust
use ic_node_rewards_canister_api::DateUtc;
use chrono::NaiveDate;

let d = DateUtc { year: Some(u32::MAX), month: Some(1), day: Some(1) };
// u32::MAX as i32 = -1; NaiveDate::from_ymd_opt(-1,1,1) = Some(...)
let naive = NaiveDate::try_from(d).unwrap(); // succeeds — BUG: should be Err
// naive = NaiveDate(-1, 1, 1)
// naive.and_hms_nano_opt(23,59,59,999_999_999).unwrap().and_utc()
//      .timestamp_nanos_opt() == None  →  .unwrap() panics
```

### Citations

**File:** rs/node_rewards/canister/api/src/lib.rs (L60-71)
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

**File:** rs/node_rewards/canister/src/canister/mod.rs (L208-214)
```rust
    fn get_rewards_table(&self, date: &NaiveDate) -> Result<NodeRewardsTable, String> {
        let registry_querier = RegistryQuerier::new(self.registry_client.clone());

        let version = registry_querier
            .version_for_timestamp_nanoseconds(last_unix_timestamp_nanoseconds(date))
            .ok_or_else(|| "Could not find registry version for timestamp".to_string())?;
        Ok(registry_querier.get_rewards_table(version))
```

**File:** rs/node_rewards/canister/src/chrono_utils.rs (L12-19)
```rust
pub fn last_unix_timestamp_nanoseconds(naive_date: &NaiveDate) -> u64 {
    naive_date
        .and_hms_nano_opt(23, 59, 59, 999_999_999)
        .unwrap()
        .and_utc()
        .timestamp_nanos_opt()
        .unwrap() as u64
}
```
