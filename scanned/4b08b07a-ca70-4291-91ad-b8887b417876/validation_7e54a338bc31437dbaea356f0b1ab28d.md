### Title
Missing Canister History Entry for Environment Variable Changes via `update_settings` - (File: `rs/execution_environment/src/canister_manager.rs`)

### Summary

The IC management canister's `update_settings` method silently applies environment variable changes to a canister's state without recording any entry in the canister history. The `canister_info` endpoint — the IC's certified public audit trail for canister lifecycle events — will not reflect these changes. A malicious controller in a multi-controller canister can silently alter environment variables that govern canister behavior, with no detectable trace via the standard audit mechanism.

### Finding Description

The `update_settings` function in `rs/execution_environment/src/canister_manager.rs` deliberately skips recording environment variable changes in canister history. The code comment reads:

> "For the sake of backward-compatibility, we do not record changes to canister environment variables in canister history. In particular, we never produce a canister history entry of the form `settings_change`." [1](#0-0) 

The entire block that would have called `canister.add_canister_change(... CanisterChangeDetails::settings_change(...))` is commented out. Only controller changes are still recorded via the legacy `controllers_change` variant: [2](#0-1) 

The `CanisterChangeDetails::settings_change` variant exists in the type system and was designed to carry both a new controllers list and an `environment_variables_hash`: [3](#0-2) 

The DID interface exposes `settings_change` as a first-class history entry type: [4](#0-3) 

The test `canister_history_tracking_env_vars_update_settings` explicitly asserts that the history count stays at `1` after an environment variable update, with the expected `settings_change` assertion commented out: [5](#0-4) 

Similarly, `canister_history_no_change_during_update_settings` confirms that an `update_settings` call carrying only environment variables produces zero new history entries: [6](#0-5) 

### Impact Explanation

Environment variables set via `update_settings` are accessible to the canister during execution and can govern security-critical behavior (e.g., upstream oracle endpoints, fee recipient addresses, access-control flags). The `canister_info` management canister call is the IC's certified, publicly queryable audit trail for canister lifecycle changes. Because environment variable mutations produce no history entry:

- The `total_num_changes` counter does not increment, defeating even count-based change detection.
- Other controllers of the same canister cannot detect the mutation via `canister_info`.
- Off-chain monitoring systems that rely on `canister_info` to track configuration drift will silently miss the change.
- The `canister_version` is bumped (via `bump_canister_version`) but no corresponding history record is written, creating an unexplained version gap that is difficult to audit. [7](#0-6) 

### Likelihood Explanation

The entry path is a standard ingress message to the management canister's `update_settings` method, callable by any controller of the target canister. No privileged subnet access, governance majority, or leaked key is required. In any canister with multiple controllers (a common production pattern for shared governance), one controller can silently reconfigure environment variables without the others being able to detect it through the standard protocol-level audit mechanism.

### Recommendation

Re-enable the commented-out `CanisterChangeDetails::settings_change` recording path in `update_settings`. When environment variables are changed (i.e., when `settings.environment_variables()` is `Some`), emit a `settings_change` history entry carrying the new `environment_variables_hash`. This is already fully modeled in the type system and the DID interface; only the runtime recording is suppressed. The "backward-compatibility" concern should be addressed by a versioned migration rather than permanently omitting the audit record. [8](#0-7) 

### Proof of Concept

1. Create a canister with two controllers, `Alice` and `Bob`.
2. `Alice` calls `update_settings` on the management canister, passing `environment_variables = [("ORACLE_URL", "https://evil.example.com")]`.
3. The call succeeds; the canister's environment variables are updated in state.
4. `Bob` calls `canister_info` to audit recent changes. The response shows `total_num_changes = 1` (only the creation entry) — the environment variable change is invisible.
5. The canister now executes against the attacker-controlled `ORACLE_URL` with no on-chain evidence of the reconfiguration.

The test `canister_history_tracking_env_vars_update_settings` in `rs/execution_environment/tests/canister_history.rs` (lines 1281–1373) mechanically demonstrates steps 2–4: environment variables are mutated, yet `history.get_total_num_changes()` remains `1`. [9](#0-8)

### Citations

**File:** rs/execution_environment/src/canister_manager.rs (L665-665)
```rust
        canister.system_state.bump_canister_version();
```

**File:** rs/execution_environment/src/canister_manager.rs (L671-710)
```rust
        // For the sake of backward-compatibility, we do not record
        // changes to canister environment variables in canister history.
        // In particular, we never produce a canister history entry of the form `settings_change`.
        /*
        match self.environment_variables_flag {
            FlagStatus::Enabled => {
                let new_environment_variables_hash = validated_settings
                    .environment_variables()
                    .map(|environment_variables| environment_variables.hash());

                if new_environment_variables_hash.is_some() || new_controllers.is_some() {
                    let available_execution_memory_change = canister.add_canister_change(
                        timestamp_nanos,
                        origin,
                        CanisterChangeDetails::settings_change(
                            new_controllers,
                            new_environment_variables_hash,
                        ),
                    );
                    round_limits
                        .subnet_available_memory
                        .update_execution_memory_unchecked(available_execution_memory_change);
                }
            }
            FlagStatus::Disabled => {
        */
        if let Some(new_controllers) = new_controllers {
            let available_execution_memory_change = canister.add_canister_change(
                timestamp_nanos,
                origin,
                CanisterChangeDetails::controllers_change(new_controllers),
            );
            round_limits
                .subnet_available_memory
                .update_execution_memory_unchecked(available_execution_memory_change);
        }
        /*
            }
        }
        */
```

**File:** rs/types/management_canister_types/src/lib.rs (L580-588)
```rust
    pub fn settings_change(
        controllers: Option<Vec<PrincipalId>>,
        environment_variables_hash: Option<[u8; HASH_LENGTH]>,
    ) -> CanisterChangeDetails {
        CanisterChangeDetails::CanisterSettingsChange(CanisterSettingsChangeRecord {
            controllers,
            environment_variables_hash,
        })
    }
```

**File:** rs/types/management_canister_types/tests/ic.did (L85-88)
```text
    settings_change: record {
        controllers : opt vec principal;
        environment_variables_hash: opt blob;
    };
```

**File:** rs/execution_environment/tests/canister_history.rs (L1281-1373)
```rust
fn canister_history_tracking_env_vars_update_settings() {
    let user_id = user_test_id(7).get();
    let intial_env_vars = EnvironmentVariables::new(BTreeMap::from([
        ("NODE_ENV".to_string(), "production".to_string()),
        ("LOG_LEVEL".to_string(), "info".to_string()),
    ]));
    let initial_env_vars_hash = intial_env_vars.hash();

    // Set up StateMachine.
    let env = setup_with_application_subnet();
    // Set time of StateMachine to current system time.
    let mut now = std::time::SystemTime::now();
    env.set_time(now);

    let canister_id = env.create_canister_with_cycles(
        None,
        INITIAL_CYCLES_BALANCE,
        Some(
            CanisterSettingsArgsBuilder::new()
                .with_controllers(vec![user_id])
                .with_environment_variables(
                    intial_env_vars
                        .iter()
                        .map(|(name, value)| EnvironmentVariable {
                            name: name.clone(),
                            value: value.clone(),
                        })
                        .collect::<Vec<_>>(),
                )
                .build(),
        ),
    );

    // Update settings with new environment variables.
    now += Duration::from_secs(5);
    env.set_time(now);
    let env_vars = EnvironmentVariables::new(BTreeMap::from([
        ("NODE_ENV".to_string(), "production".to_string()),
        ("LOG_LEVEL".to_string(), "debug".to_string()),
    ]));

    env.execute_ingress_as(
        user_id,
        ic00::IC_00,
        Method::UpdateSettings,
        UpdateSettingsArgs {
            canister_id: canister_id.into(),
            sender_canister_version: Some(2),
            settings: CanisterSettingsArgsBuilder::new()
                .with_environment_variables(
                    env_vars
                        .iter()
                        .map(|(name, value)| EnvironmentVariable {
                            name: name.clone(),
                            value: value.clone(),
                        })
                        .collect::<Vec<_>>(),
                )
                .build(),
        }
        .encode(),
    )
    .unwrap();

    /*
    // Expected canister history change after update settings.
    let env_vars_hash = env_vars.hash();
    let reference_change = CanisterChange::new(
        now.duration_since(UNIX_EPOCH).unwrap().as_nanos() as u64,
        1,
        CanisterChangeOrigin::from_user(user_id),
        CanisterChangeDetails::settings_change(None, Some(env_vars_hash)),
    );
    */

    // Verify canister history is not updated.
    let history = env.get_canister_history(canister_id);
    assert_eq!(history.get_total_num_changes(), 1);
    let changes = history
        .get_changes(history.get_total_num_changes() as usize)
        .map(|c| (**c).clone())
        .collect::<Vec<CanisterChange>>();
    assert_eq!(
        changes[0].details(),
        &CanisterChangeDetails::canister_creation(vec![user_id], Some(initial_env_vars_hash))
    );
    //assert_eq!(changes[1], reference_change);

    // Verify the environment variables of the canister state.
    let state = env.get_latest_state();
    let canister_state = state.canister_state(&canister_id).unwrap();
    assert_eq!(canister_state.system_state.environment_variables, env_vars);
}
```

**File:** rs/execution_environment/tests/canister_history.rs (L1416-1427)
```rust
    // Verify canister history contains only the canister creation change.
    let history = env.get_canister_history(canister_id);
    assert_eq!(history.get_total_num_changes(), 1);
    let changes = history
        .get_changes(history.get_total_num_changes() as usize)
        .map(|c| (**c).clone())
        .collect::<Vec<CanisterChange>>();
    assert_eq!(
        changes[0].details(),
        &CanisterChangeDetails::canister_creation(vec![user_id], None)
    );
}
```
