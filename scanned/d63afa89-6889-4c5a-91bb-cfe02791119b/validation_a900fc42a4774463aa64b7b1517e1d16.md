### Title
SNS Governance Proceeds with Stale `cached_upgrade_steps` Without Freshness Enforcement - (`rs/sns/governance/src/cached_upgrade_steps.rs`)

### Summary
SNS Governance caches upgrade steps fetched from SNS-W in `cached_upgrade_steps`. Core operations — proposal validation (`validate_new_target_version`) and automatic upgrade initiation (`initiate_upgrade_if_sns_behind_target_version`) — consume this cache without any guard verifying the cache is fresh. The cache can be up to `UPGRADE_STEPS_INTERVAL_REFRESH_BACKOFF_SECONDS` (3600 seconds) stale. This is a direct analog to M-07: operations that depend on a synced external state proceed against a potentially out-of-date local snapshot, with no on-chain enforcement of freshness.

### Finding Description

`GovernancePb::validate_new_target_version` is called during `AdvanceSnsTargetVersion` proposal validation. It reads `cached_upgrade_steps` via `cached_upgrade_steps_or_err()` with no check that the cache was recently refreshed: [1](#0-0) 

`Governance::initiate_upgrade_if_sns_behind_target_version` (called from the heartbeat when `automatically_advance_target_version` is enabled) calls `get_or_reset_upgrade_steps` which also reads from the local cache without a freshness guard: [2](#0-1) 

The cache staleness window is controlled by `should_refresh_cached_upgrade_steps`, which only signals that a refresh is *needed* — it does not block operations from using the stale cache: [3](#0-2) 

The cache is refreshed asynchronously via heartbeat. Between refreshes, `cached_upgrade_steps` can diverge from the live SNS-W state for up to one hour. [4](#0-3) 

### Impact Explanation

**Governance authorization bug / message-routing ordering bug.**

If SNS-W removes or supersedes a version (e.g., due to a discovered vulnerability in a canister WASM), SNS Governance's local cache may still list that version as a valid upgrade target for up to one hour. During this window:

1. An `AdvanceSnsTargetVersion` proposal targeting the now-invalid version passes on-chain validation (because validation reads only the stale local cache).
2. If `automatically_advance_target_version` is enabled, the heartbeat autonomously initiates an upgrade to the stale target without any user action.
3. The upgrade call to SNS-W fails (the WASM is no longer available), leaving the SNS in a `pending_version` state until the `mark_failed_at_seconds` timeout (5 minutes), after which it recovers.

The practical impact is a temporary, recoverable DoS of the SNS upgrade mechanism and the possibility of governance proposals being accepted against a state that SNS-W has already invalidated.

### Likelihood Explanation

**Low-to-medium.** The stale window is bounded at one hour. Exploitation requires either (a) a neuron holder submitting a proposal during the stale window, or (b) `automatically_advance_target_version` being enabled, in which case no user action is needed — the heartbeat triggers the issue automatically. The latter path requires no privileged access beyond the SNS's own configuration.

### Recommendation

Add a freshness guard before consuming `cached_upgrade_steps` in both `validate_new_target_version` and `initiate_upgrade_if_sns_behind_target_version`. Concretely:

- In `validate_new_target_version`, check `should_refresh_cached_upgrade_steps()` and return an error (e.g., "Upgrade steps cache is stale; please wait for the next refresh") if the cache is not fresh, rather than proceeding with potentially outdated data.
- In `initiate_upgrade_if_sns_behind_target_version`, skip the upgrade attempt if the cache is stale and log a warning, analogous to how the function already skips when an upgrade is already in progress.

This mirrors the M-07 recommendation: add a modifier/guard to all functions that interact with the cached external state to require that the cache is synced before proceeding.

### Proof of Concept

**Scenario A — via governance proposal (any neuron holder):**

1. SNS-W removes version `V_bad` from its upgrade path (e.g., it contains a vulnerability).
2. SNS Governance's `cached_upgrade_steps` still contains `V_bad` (cache is < 3600 s old).
3. A neuron holder submits `AdvanceSnsTargetVersion { new_target: V_bad }`.
4. `validate_new_target_version` reads the stale cache, finds `V_bad` in `upgrade_steps`, and returns `Ok`.
5. The proposal is adopted and executed; `target_version` is set to `V_bad`.
6. `initiate_upgrade_if_sns_behind_target_version` fires, calls `upgrade_sns_framework_canister` with `V_bad`'s WASM hash.
7. SNS-W returns an error (version not found); the upgrade fails; `pending_version` is cleared after 5 minutes. [5](#0-4) 

**Scenario B — automatic (no user action, `automatically_advance_target_version = true`):**

1. SNS-W publishes a new version `V_new`.
2. SNS Governance's cache is stale and does not yet contain `V_new`.
3. Heartbeat calls `refresh_cached_upgrade_steps` → `try_temporarily_lock_refresh_cached_upgrade_steps` → `get_or_reset_upgrade_steps` using the stale `deployed_version`.
4. The stale cache is used to set `target_version` to a version that may no longer be the correct next step.
5. Upgrade is initiated against the stale target; SNS-W may reject it. [6](#0-5)

### Citations

**File:** rs/sns/governance/src/cached_upgrade_steps.rs (L417-436)
```rust
    pub(crate) fn get_or_reset_upgrade_steps(
        &mut self,
        current_version: &Version,
    ) -> CachedUpgradeSteps {
        let reason = if let Some(cached_upgrade_steps_pb) = &self.proto.cached_upgrade_steps {
            match CachedUpgradeSteps::try_from(cached_upgrade_steps_pb)
                .and_then(|cached_upgrade_steps| cached_upgrade_steps.take_from(current_version))
            {
                Ok(upgrade_steps) => {
                    // Happy case.
                    return upgrade_steps;
                }
                Err(err) => err,
            }
        } else {
            "Initializing the cache".to_string()
        };

        self.reset_cached_upgrade_steps(current_version, reason)
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

**File:** rs/sns/governance/src/cached_upgrade_steps.rs (L472-532)
```rust
    /// Attempts to refresh the cached_upgrade_steps field and (if this SNS wants automatic
    /// deployment of upgrades), also the target_version.
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

**File:** rs/sns/governance/src/cached_upgrade_steps.rs (L550-578)
```rust
    pub(crate) fn validate_new_target_version<V>(
        &self,
        new_target: Option<V>,
    ) -> Result<
        (
            /* pending_upgrade_steps */ CachedUpgradeSteps,
            /* valid_target_version */ Version,
        ),
        String,
    >
    where
        Version: TryFrom<V>,
        <Version as TryFrom<V>>::Error: ToString,
    {
        let deployed_version = self.deployed_version_or_err()?;

        let cached_upgrade_steps = self.cached_upgrade_steps_or_err()?;

        let upgrade_steps = cached_upgrade_steps.take_from(&deployed_version);
        let upgrade_steps = match upgrade_steps {
            Ok(upgrade_steps) if upgrade_steps.has_pending_upgrades() => upgrade_steps,
            _ => {
                return Err(format!(
                    "Currently, the SNS does not have pending upgrades. \
                     You may need to wait for the upgrade steps to be refreshed. \
                     This shouldn't take more than {UPGRADE_STEPS_INTERVAL_REFRESH_BACKOFF_SECONDS} seconds."
                ));
            }
        };
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L1694-1708)
```text
  // The sns's local cache of the upgrade steps recieved from SNS-W.
  message CachedUpgradeSteps {
    // The upgrade steps that have been returned from SNS-W the last time we
    // called list_upgrade_steps.
    Governance.Versions upgrade_steps = 1;
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

**File:** rs/sns/governance/src/governance.rs (L5567-5652)
```rust
    /// Checks if an automatic upgrade is needed and initiates it.
    /// An automatic upgrade is needed if `target_version` is set to a future version on the upgrade path
    async fn initiate_upgrade_if_sns_behind_target_version(&mut self) {
        // Check that no upgrades are in progress
        if self.check_no_upgrades_in_progress(None).is_err() {
            // An upgrade is already in progress
            return;
        }

        let deployed_version = match self.get_or_reset_deployed_version().await {
            Ok(deployed_version) => deployed_version,
            Err(err) => {
                log!(ERROR, "Cannot get or reset deployed version: {}", err);
                return;
            }
        };

        let upgrade_steps = self.get_or_reset_upgrade_steps(&deployed_version);

        let Some(target_version) = self.proto.target_version.clone() else {
            return;
        };

        // Find the target position of the target version
        if !upgrade_steps.contains(&target_version) {
            let message = format!(
                "Target version {target_version} is not on the upgrade path {upgrade_steps:?}"
            );
            self.invalidate_target_version(message);
            return;
        };

        // If the target version is the same as the deployed version, there is nothing to do.
        if upgrade_steps.is_current(&target_version) {
            return;
        }

        let Some(next_version) = upgrade_steps.next() else {
            // This should be impossible because we already established that
            // `target_version` ∈ `upgrade_steps` \ { `current_version` }.
            // However, if this code path would be taken due to a bug, we would interpret
            // the situation as "no more work."
            log!(
                ERROR,
                "Taking a code path that was supposed to be impossible. \
                 target_version = {:?}, upgrade_steps = {:?}.",
                target_version,
                upgrade_steps,
            );
            return;
        };

        let (canister_type, wasm_hash) =
            match canister_type_and_wasm_hash_for_upgrade(&deployed_version, next_version) {
                Ok((canister_type, wasm_hash)) => (canister_type, wasm_hash),

                Err(err) => {
                    let message = format!("Upgrade attempt failed: {err}");
                    log!(ERROR, "{}", message);
                    self.invalidate_target_version(message);
                    return;
                }
            };

        self.push_to_upgrade_journal(upgrade_journal_entry::UpgradeStarted::from_behind_target(
            deployed_version.clone(),
            next_version.clone(),
        ));

        self.proto.pending_version = Some(PendingVersion {
            target_version: Some(next_version.clone()),
            mark_failed_at_seconds: self.env.now() + 5 * 60,
            checking_upgrade_lock: 0,
            proposal_id: None,
        });

        println!("Initiating upgrade to version: {next_version:?}");
        let upgrade_attempt = self
            .upgrade_sns_framework_canister(wasm_hash, canister_type)
            .await;
        if let Err(err) = upgrade_attempt {
            let message = format!("Upgrade attempt failed: {err}");
            log!(ERROR, "{}", message);
            self.proto.pending_version = None;
            self.invalidate_target_version(message);
        }
```
