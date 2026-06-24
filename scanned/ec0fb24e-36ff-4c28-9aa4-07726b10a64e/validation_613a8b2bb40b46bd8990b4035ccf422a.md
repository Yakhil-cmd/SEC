### Title
Unprivileged Caller Can Trigger Canister Trap via `.unwrap()` on `None` in `get_rewardable_nodes_per_provider` - (`rs/node_rewards/canister/src/registry_querier.rs`)

### Summary

`get_rewardable_nodes_per_provider` unconditionally calls `.unwrap()` on the result of `version_for_timestamp_nanoseconds`, which returns `None` when the queried timestamp precedes all entries in `timestamp_to_versions_map`. The upstream `validate_reward_period` guard does not prevent dates before the earliest registry entry from being queried. An unprivileged caller can trigger a canister trap by supplying a `from_day`/`to_day` in year 1970.

### Finding Description

`version_for_timestamp_nanoseconds` performs a `range(..=ts).next_back()` on the `timestamp_to_versions_map`: [1](#0-0) 

If `ts` is smaller than every key in the map (e.g., a 1970 date when all registry versions were recorded in 2024), the range is empty and the method returns `None`.

`get_rewardable_nodes_per_provider` calls this and immediately `.unwrap()`s the result: [2](#0-1) 

`validate_reward_period` only checks that `to_date <= last_day_synced` and `to_date < today`: [3](#0-2) 

There is no lower-bound check on the queried date. A date of `1970-01-01` satisfies all three guards (it is before today, and before any `last_day_synced` value set in 2024+). `last_unix_timestamp_nanoseconds` for `1970-01-01` returns ≈86 399 999 999 999 ns: [4](#0-3) 

This is orders of magnitude smaller than any 2024 registry timestamp (≈1.7 × 10¹⁸ ns), so `version_for_timestamp_nanoseconds` returns `None` and `.unwrap()` panics.

By contrast, the `PerformanceBasedAlgorithmInputProvider::get_rewards_table` implementation correctly handles the `None` case with `.ok_or_else(...)`: [5](#0-4) 

`get_rewardable_nodes_per_provider` lacks this same defensive handling.

The public entry point `get_node_providers_rewards` is callable by any unprivileged principal: [6](#0-5) 

### Impact Explanation

A canister trap rolls back the message and returns an error to the caller; the canister itself survives. The impact is a **forced message-level trap** on every `get_node_providers_rewards` or `get_node_providers_rewards_calculation` call that includes a date before the earliest registry entry. Any caller can repeatedly trigger this at will, making those API endpoints unreliable for legitimate users during the attack window.

### Likelihood Explanation

The exploit requires no privilege, no key material, and no subnet-majority corruption. The attacker only needs to know the canister ID and submit a valid Candid-encoded request with a historical date. The precondition (at least one registry sync having occurred) is always true in production. Likelihood is **high**.

### Recommendation

Replace the `.unwrap()` at line 163 with proper error propagation, matching the pattern already used in `get_rewards_table`:

```rust
// registry_querier.rs, get_rewardable_nodes_per_provider
let registry_version = self
    .version_for_timestamp_nanoseconds(last_unix_timestamp_nanoseconds(date))
    .ok_or_else(|| {
        ic_types::registry::RegistryClientError::VersionNotAvailable {
            version: RegistryVersion::new(0),
        }
    })?;
```

Or return a domain-specific `Err(String)` and change the function signature accordingly. Additionally, add a lower-bound check in `validate_reward_period` that rejects any date whose `last_unix_timestamp_nanoseconds` is before the smallest key in `timestamp_to_versions_map`.

### Proof of Concept

1. Deploy the node rewards canister.
2. Trigger a registry sync so that `last_day_synced` is set to a 2024 date and `timestamp_to_versions_map` contains only 2024 entries.
3. Call `get_node_providers_rewards` with `from_day = {year:1970, month:1, day:1}` and `to_day = {year:1970, month:1, day:1}`.
4. `validate_reward_period` passes (1970-01-01 < today, 1970-01-01 ≤ last_day_synced).
5. `calculate_rewards_for_date` → `get_rewardable_nodes` → `get_rewardable_nodes_per_provider` is called.
6. `last_unix_timestamp_nanoseconds(1970-01-01)` ≈ 86 399 999 999 999 ns < smallest 2024 map key.
7. `version_for_timestamp_nanoseconds` returns `None`; `.unwrap()` panics; canister trap is returned to caller.

### Citations

**File:** rs/node_rewards/canister/src/registry_querier.rs (L31-38)
```rust
    pub fn version_for_timestamp_nanoseconds(&self, ts: UnixTsNanos) -> Option<RegistryVersion> {
        self.registry_client
            .timestamp_to_versions_map()
            .range(..=ts)
            .next_back()
            .and_then(|(_, versions)| versions.iter().max())
            .cloned()
    }
```

**File:** rs/node_rewards/canister/src/registry_querier.rs (L161-163)
```rust
        let registry_version = self
            .version_for_timestamp_nanoseconds(last_unix_timestamp_nanoseconds(date))
            .unwrap();
```

**File:** rs/node_rewards/canister/src/canister/mod.rs (L153-178)
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
```

**File:** rs/node_rewards/canister/src/canister/mod.rs (L208-215)
```rust
    fn get_rewards_table(&self, date: &NaiveDate) -> Result<NodeRewardsTable, String> {
        let registry_querier = RegistryQuerier::new(self.registry_client.clone());

        let version = registry_querier
            .version_for_timestamp_nanoseconds(last_unix_timestamp_nanoseconds(date))
            .ok_or_else(|| "Could not find registry version for timestamp".to_string())?;
        Ok(registry_querier.get_rewards_table(version))
    }
```

**File:** rs/node_rewards/canister/src/canister/mod.rs (L314-320)
```rust
    pub async fn get_node_providers_rewards(
        canister: &'static LocalKey<RefCell<NodeRewardsCanister>>,
        request: GetNodeProvidersRewardsRequest,
    ) -> GetNodeProvidersRewardsResponse {
        let from_date = NaiveDate::try_from(request.from_day)?;
        let to_date = NaiveDate::try_from(request.to_day)?;
        canister.with_borrow(|c| c.validate_reward_period(from_date, to_date))?;
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
