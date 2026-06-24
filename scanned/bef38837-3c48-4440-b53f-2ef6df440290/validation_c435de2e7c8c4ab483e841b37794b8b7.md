### Title
Premature `requested_timestamp_seconds` Update Suppresses Upgrade-Step Refresh Retries After Failed SNS-W Call - (File: rs/sns/governance/src/cached_upgrade_steps.rs)

### Summary

The SNS Governance canister's `CachedUpgradeSteps` mechanism uses `requested_timestamp_seconds` as a rate-limiting gate to avoid calling `SNS-W.list_upgrade_steps` too frequently. The function `try_temporarily_lock_refresh_cached_upgrade_steps()` writes this timestamp to persisted state **before** the actual async refresh call (`refresh_cached_upgrade_steps`) completes. If the refresh fails, the timestamp is already committed, and `should_refresh_cached_upgrade_steps()` will suppress all retry attempts for the full `UPGRADE_STEPS_INTERVAL_REFRESH_BACKOFF_SECONDS` window — leaving the upgrade-steps cache stale and blocking automatic SNS version advancement.

### Finding Description

The call sequence is:

1. `try_temporarily_lock_refresh_cached_upgrade_steps()` is called. It immediately sets `cached_upgrade_steps.requested_timestamp_seconds = self.env.now()` and writes the mutated proto back to `self.proto.cached_upgrade_steps`. [1](#0-0) 

2. The caller then invokes `refresh_cached_upgrade_steps(deployed_version)`. Inside, `get_upgrade_steps()` makes an async cross-canister call to `SNS_WASM_CANISTER_ID`. If that call fails (network error, canister reject, decode error), the function logs the error and **returns early without touching `self.proto.cached_upgrade_steps`**. [2](#0-1) 

3. On the next periodic task, `should_refresh_cached_upgrade_steps()` reads `requested_timestamp_seconds` from the proto and compares it to `now`. Because the timestamp was already stamped in step 1, the backoff check fires and the function returns `false`, suppressing any retry. [3](#0-2) 

The proto comment itself acknowledges the asymmetry: `requested_timestamp_seconds` can exceed `response_timestamp_seconds` because it is written at request time, not response time. [4](#0-3) 

The `get_upgrade_steps()` helper in `sns_upgrade.rs` captures `requested_timestamp_seconds = env.now()` before the await, so even a successful path records the pre-call time — but on failure the helper returns `Err` and the proto is never updated with a fresh `response_timestamp_seconds`. [5](#0-4) 

### Impact Explanation

After any transient failure of the `SNS-W.list_upgrade_steps` call, the SNS Governance canister will not attempt another refresh for the entire `UPGRADE_STEPS_INTERVAL_REFRESH_BACKOFF_SECONDS` period. During this window:

- `proto.cached_upgrade_steps` retains its pre-failure content (potentially stale or empty upgrade steps).
- SNS instances with `automatically_advance_target_version = true` will not advance to newly published versions, stalling the SNS upgrade pipeline.
- Governance proposals that validate against `cached_upgrade_steps` (e.g., `validate_new_target_version`) may incorrectly reject valid target versions that appeared in SNS-W after the stale snapshot. [6](#0-5) 

### Likelihood Explanation

The SNS-W cross-canister call can fail due to transient replica-level errors, canister upgrades of SNS-W itself, or malformed responses (e.g., duplicate versions, missing fields). The `try_from_sns_w_response` parser already demonstrates several realistic failure modes (empty steps, duplicate versions) that return `Err` and trigger the early-return path. [7](#0-6) 

Each such failure silently locks out retries for the full backoff window, compounding the delay if failures are intermittent.

### Recommendation

Move the `requested_timestamp_seconds` stamp to **after** a successful response is received and stored, mirroring how `response_timestamp_seconds` is handled. Concretely:

- Remove the pre-call timestamp write from `try_temporarily_lock_refresh_cached_upgrade_steps()` (or use a separate in-memory inflight flag that is not persisted and does not affect the backoff gate).
- In `refresh_cached_upgrade_steps()`, set `requested_timestamp_seconds` only when `new_cache` is about to be committed to `self.proto.cached_upgrade_steps`.

This ensures that a failed refresh leaves `requested_timestamp_seconds` unchanged, allowing the next periodic task to retry immediately.

### Proof of Concept

```
1. SNS Governance canister has cached_upgrade_steps with requested_timestamp_seconds = T0 (old).
2. Periodic task fires; should_refresh_cached_upgrade_steps() returns true (T_now - T0 >= backoff).
3. try_temporarily_lock_refresh_cached_upgrade_steps() writes requested_timestamp_seconds = T_now to proto.
4. refresh_cached_upgrade_steps() calls SNS-W; SNS-W returns an error (e.g., duplicate versions).
5. refresh_cached_upgrade_steps() logs error and returns — proto.cached_upgrade_steps unchanged except for the timestamp written in step 3.
6. Next periodic task fires at T_now + ε; should_refresh_cached_upgrade_steps() computes T_now + ε - T_now = ε < UPGRADE_STEPS_INTERVAL_REFRESH_BACKOFF_SECONDS → returns false.
7. No retry occurs for the full backoff window; upgrade steps cache remains stale.
``` [1](#0-0) [8](#0-7)

### Citations

**File:** rs/sns/governance/src/cached_upgrade_steps.rs (L213-253)
```rust
impl CachedUpgradeSteps {
    pub fn try_from_sns_w_response(
        sns_w_response: ListUpgradeStepsResponse,
        requested_timestamp_seconds: u64,
        response_timestamp_seconds: u64,
    ) -> Result<Self, String> {
        let response_str = format!("{sns_w_response:?}");

        let ListUpgradeStepsResponse { steps } = sns_w_response;

        let versions: Vec<Version> = steps
            .into_iter()
            .map(|list_upgrade_step| match list_upgrade_step {
                ListUpgradeStep {
                    version: Some(version),
                } => Ok(version.into()),
                _ => Err(format!(
                    "SnsW.list_upgrade_steps response had invalid fields: {response_str}"
                )),
            })
            .collect::<Result<_, _>>()?;

        let versions = Versions { versions };

        versions.validate_no_duplicates().map_err(|err| {
            format!("ListUpgradeStepsResponse.steps must not contain duplicates: {err}")
        })?;

        let mut versions = versions.versions.into_iter();

        let Some(current_version) = versions.next() else {
            return Err("ListUpgradeStepsResponse.steps must not be empty.".to_string());
        };

        Ok(Self {
            current_version,
            subsequent_versions: versions.collect(),
            requested_timestamp_seconds,
            response_timestamp_seconds,
        })
    }
```

**File:** rs/sns/governance/src/cached_upgrade_steps.rs (L274-295)
```rust
    pub fn contains(&self, version: &Version) -> bool {
        &self.current_version == version || self.subsequent_versions.contains(version)
    }

    /// Returns whether `left` is before or equal to `right` in `self`, in the `Ok` result.
    ///
    /// Returns `Err` if at least one of the versions `left` or `right` are not in `self`.
    pub fn contains_in_order(&self, left: &Version, right: &Version) -> Result<bool, String> {
        if !self.contains(left) {
            return Err(format!("{self:?} does not contain {left:?}"));
        }
        if !self.contains(right) {
            return Err(format!("{self:?} does not contain {right:?}"));
        }

        // Check if we have `current_version` -> ... -> `left` -> `right` -> ...
        let upgrade_steps_starting_from_left = self.clone().take_from(left)?;

        let contains_in_order = upgrade_steps_starting_from_left.contains(right);

        Ok(contains_in_order)
    }
```

**File:** rs/sns/governance/src/cached_upgrade_steps.rs (L438-453)
```rust
    pub fn try_temporarily_lock_refresh_cached_upgrade_steps(&mut self) -> Result<Version, String> {
        let deployed_version = self
            .proto
            .deployed_version
            .clone()
            .ok_or("Cannot lock refresh_cached_upgrade_steps: deployed_version not set.")?;

        let mut cached_upgrade_steps = self.get_or_reset_upgrade_steps(&deployed_version);

        // Lock the upgrade mechanism.
        cached_upgrade_steps.requested_timestamp_seconds = self.env.now();
        let cached_upgrade_steps = CachedUpgradeStepsPb::from(cached_upgrade_steps);
        self.proto.cached_upgrade_steps = Some(cached_upgrade_steps);

        Ok(deployed_version)
    }
```

**File:** rs/sns/governance/src/cached_upgrade_steps.rs (L455-470)
```rust
    pub fn should_refresh_cached_upgrade_steps(&mut self) -> bool {
        let now = self.env.now();

        if let Some(ref cached_upgrade_steps) = self.proto.cached_upgrade_steps {
            let requested_timestamp_seconds = cached_upgrade_steps
                .requested_timestamp_seconds
                .unwrap_or(0);
            if now.saturating_sub(requested_timestamp_seconds)
                < UPGRADE_STEPS_INTERVAL_REFRESH_BACKOFF_SECONDS
            {
                return false;
            }
        }

        true
    }
```

**File:** rs/sns/governance/src/cached_upgrade_steps.rs (L474-532)
```rust
    pub async fn refresh_cached_upgrade_steps(&mut self, deployed_version: Version) {
        let sns_governance_canister_id = self.env.canister_id().get();

        let upgrade_steps = crate::sns_upgrade::get_upgrade_steps(
            &*self.env,
            deployed_version,
            sns_governance_canister_id,
        )
        .await;

        let upgrade_steps = match upgrade_steps {
            Ok(upgrade_steps) => upgrade_steps,
            Err(err) => {
                log!(ERROR, "Cannot refresh cached_upgrade_steps: {}", err);
                return;
            }
        };

        if self.should_automatically_advance_target_version()
            && upgrade_steps.has_pending_upgrades()
        {
            let new_target = upgrade_steps.last().clone();

            {
                let old_version = self.proto.target_version.clone();
                let new_target = new_target.clone();
                if old_version.as_ref() != Some(&new_target) {
                    self.push_to_upgrade_journal(upgrade_journal_entry::TargetVersionSet::new(
                        old_version,
                        new_target,
                        true,
                    ));
                }
            }

            self.proto.target_version.replace(new_target);
        }

        // This copy of the data would go to the upgrade journal for auditability.
        let versions = upgrade_steps.clone().into_iter().collect();

        // This copy would be stored in the cache.
        let new_cache = CachedUpgradeStepsPb::from(upgrade_steps);

        let received_upgrade_steps_same_as_previous = self
            .proto
            .cached_upgrade_steps
            .as_ref()
            .map(|cache| cache.upgrade_steps == new_cache.upgrade_steps)
            .unwrap_or_default();

        if !received_upgrade_steps_same_as_previous {
            self.push_to_upgrade_journal(upgrade_journal_entry::UpgradeStepsRefreshed::new(
                versions,
            ));
        }

        self.proto.cached_upgrade_steps.replace(new_cache);
    }
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L1699-1708)
```text
    // The timestamp of the request we sent to list_upgrade_steps.
    // It's possible that this is greater than the response_timestamp_seconds, because
    // we update it as soon as we send the request, and only update the
    // response_timestamp and the upgrade_steps when we receive the response.
    // The primary use of this is that we can avoid calling list_upgrade_steps
    // more frequently than necessary.
    optional uint64 requested_timestamp_seconds = 2;
    // The timestamp of the response we received from list_upgrade_steps (stored in upgrade_steps).
    optional uint64 response_timestamp_seconds = 3;
  }
```

**File:** rs/sns/governance/src/sns_upgrade.rs (L293-311)
```rust
    let requested_timestamp_seconds = env.now();

    let response = env
        .call_canister(SNS_WASM_CANISTER_ID, "list_upgrade_steps", arg)
        .await
        .map_err(|err| format!("Request failed for get_next_sns_version: {err:?}"))?;

    let response = Decode!(&response, ListUpgradeStepsResponse).map_err(|err| {
        format!("Could not decode the response from SnsW.list_upgrade_steps: {err}")
    })?;

    let response_timestamp_seconds = env.now();

    CachedUpgradeSteps::try_from_sns_w_response(
        response,
        requested_timestamp_seconds,
        response_timestamp_seconds,
    )
}
```
