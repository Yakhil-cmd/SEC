### Title
Rate-Limit Canister Risky Multi-Step Initialization Leaves Write Access Permanently Disabled - (File: rs/boundary_node/rate_limits/canister/canister.rs)

### Summary
The rate-limit canister's `authorized_principal` field in `InitArg` is optional (`opt principal`). When the canister is deployed without setting it, the canister enters a permanently non-functional state for all write operations (`add_config`, `disclose_rules`). The only recovery path is a second privileged action — an NNS governance upgrade proposal — mirroring the external report's multi-step initialization risk exactly.

### Finding Description
The `InitArg` type declares `authorized_principal` as optional: [1](#0-0) 

In `init()`, if `authorized_principal` is `None`, the field is simply skipped and never stored: [2](#0-1) 

The `inspect_message` hook computes `has_full_access` as `Some(caller_id) == authorized_principal`. When `authorized_principal` is `None`, this comparison is `Some(X) == None`, which is always `false` for every possible caller: [3](#0-2) 

Consequently, `has_full_access` is `false` for all ingress callers, and `add_config` / `disclose_rules` are rejected at the pre-consensus phase: [4](#0-3) 

Even for inter-canister calls (which bypass `inspect_message`), the `AccessLevelResolver` in `access_control.rs` uses `if let Some(authorized_principal) = ...`, which never matches when the stored value is `None`, so `FullAccess` is never granted: [5](#0-4) 

Both `add_config` and `disclose_rules` require `FullAccess` and return `Unauthorized` otherwise: [6](#0-5) 

The `post_upgrade` hook simply re-runs `init`, so an upgrade that again passes `authorized_principal: None` preserves the broken state: [7](#0-6) 

The integration test explicitly documents and exercises this two-step deployment pattern — install without `authorized_principal`, assert all writes fail, then upgrade via NNS governance proposal to set it: [8](#0-7) [9](#0-8) 

### Impact Explanation
The rate-limit canister is the sole mechanism by which DFINITY can push rate-limiting rules to API boundary nodes to protect the IC during live incidents. When deployed without `authorized_principal`, the canister is completely write-locked: no rate-limit rules can be added or disclosed. The IC's boundary-node rate-limiting protection is entirely unavailable until a second NNS governance upgrade proposal is submitted, voted on, and executed. Any incident occurring during this window cannot be mitigated via the rate-limit canister.

### Likelihood Explanation
The two-step initialization is an explicitly documented and tested deployment pattern in the codebase. The `authorized_principal` field is intentionally optional, meaning any deployment proposal that omits it (accidentally or due to operational error) silently produces a non-functional canister. The integration test at `rs/tests/boundary_nodes/rate_limit_canister_test.rs` confirms this is a reachable, reproducible state.

### Recommendation
1. Make `authorized_principal` a required field in `InitArg` (change `opt principal` to `principal` in the Candid interface), so the canister cannot be installed or upgraded into a write-locked state.
2. Alternatively, apply a factory/deployer pattern analogous to the external report's recommendation: have the deployer atomically set `authorized_principal` as part of a single install transaction, preventing any intermediate incomplete state.
3. If optionality must be preserved for upgrade compatibility, add an explicit guard in `init` that traps when `authorized_principal` is `None` on first install (i.e., when `get_version()` is `None`), ensuring the canister never starts in a non-functional state.

### Proof of Concept
1. Deploy the rate-limit canister with `authorized_principal: None`:
   ```
   InitArg { registry_polling_period_secs: 60, authorized_principal: None }
   ```
2. Attempt to call `add_config` from any principal (including the deployer). The `inspect_message` hook evaluates `Some(caller) == None` → `false` → traps with `"message_inspection_failed: unauthorized caller"`.
3. Attempt the same call as an inter-canister call. `AccessLevelResolver::get_access_level()` evaluates `if let Some(p) = None` → falls through → returns `RestrictedRead`. `WithAuthorization::add_config` returns `Err(AddConfigError::Unauthorized)`.
4. The canister remains write-locked indefinitely. The only recovery is an NNS governance upgrade proposal that passes a non-`None` `authorized_principal` — a second privileged step that mirrors the proxy-ownership-transfer pattern described in the external report. [10](#0-9) [11](#0-10)

### Citations

**File:** rs/boundary_node/rate_limits/canister/interface.did (L69-72)
```text
type InitArg = record {
  authorized_principal: opt principal; // Principal authorized to perform write operations, such as adding configurations and disclosing rules
  registry_polling_period_secs: nat64; // IDs of existing API boundary nodes are polled from the registry with this periodicity
};
```

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L40-46)
```rust
    let (has_full_access, has_full_read_access) = with_canister_state(|state| {
        let authorized_principal = state.get_authorized_principal();
        (
            Some(caller_id) == authorized_principal,
            state.is_api_boundary_node_principal(&caller_id),
        )
    });
```

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L56-61)
```rust
    } else if UPDATE_METHODS.contains(&called_method.as_str()) {
        if has_full_access {
            ic_cdk::api::call::accept_message();
        } else {
            ic_cdk::api::trap("message_inspection_failed: unauthorized caller");
        }
```

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L70-97)
```rust
// Run when the canister is first installed
#[init]
fn init(init_arg: InitArg) {
    let current_time = ic_cdk::api::time();
    with_canister_state(|state| {
        // Set authorized principal, which performs write operations, such as adding new configurations
        if let Some(principal) = init_arg.authorized_principal {
            state.set_authorized_principal(principal);
        }
        // Initialize config only on the very first invocation
        if state.get_version().is_none() {
            init_version_and_config(current_time, state.clone());
        }
        // Spawn periodic job of fetching latest API boundary node topology
        // API boundary nodes are authorized readers of all config rules (including not yet disclosed ones)
        periodically_poll_api_boundary_nodes(
            init_arg.registry_polling_period_secs,
            Arc::new(state),
        );
    });
    // Update metric.
    METRICS.with(|cell| {
        let mut cell = cell.borrow_mut();
        cell.last_canister_change_time
            .borrow_mut()
            .set(current_time as i64);
    });
}
```

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L100-104)
```rust
#[post_upgrade]
fn post_upgrade(init_arg: InitArg) {
    // Run the same initialization logic
    init(init_arg);
}
```

**File:** rs/boundary_node/rate_limits/canister/access_control.rs (L38-55)
```rust
impl<R: CanisterApi> ResolveAccessLevel for AccessLevelResolver<R> {
    fn get_access_level(&self) -> AccessLevel {
        if let Some(authorized_principal) = self.canister_api.get_authorized_principal()
            && self.caller_id == authorized_principal
        {
            return AccessLevel::FullAccess;
        }

        let has_full_read_access = self
            .canister_api
            .is_api_boundary_node_principal(&self.caller_id);

        if has_full_read_access {
            return AccessLevel::FullRead;
        }

        AccessLevel::RestrictedRead
    }
```

**File:** rs/boundary_node/rate_limits/canister/access_control.rs (L72-84)
```rust
impl<T: AddsConfig, R: ResolveAccessLevel> AddsConfig for WithAuthorization<T, R> {
    fn add_config(
        &self,
        input_config: rate_limits_api::InputConfig,
        time: Timestamp,
    ) -> Result<(), AddConfigError> {
        // Only privileged users can perform this operation
        if self.access_resolver.get_access_level() == AccessLevel::FullAccess {
            // Perform the inner call only if authorized.
            return self.inner.add_config(input_config, time);
        }
        Err(AddConfigError::Unauthorized)
    }
```

**File:** rs/tests/boundary_nodes/rate_limit_canister_test.rs (L125-149)
```rust
    let args = Encode!(&InitArg {
        registry_polling_period_secs: 5,
        authorized_principal: None,
    })
    .unwrap();

    info!(
        &logger,
        "Installing rate-limit canister wasm (with unset authorized_principal)..."
    );

    install_rust_canister_from_path(&mut rate_limit_canister, path_to_wasm, Some(args)).await;

    info!(
        &logger,
        "Rate-limit canister with id={rate_limit_id} installed successfully"
    );

    let root = Canister::new(&nns, ROOT_CANISTER_ID);

    // set the root principal as the controller of the canister
    rate_limit_canister
        .set_controller(ROOT_CANISTER_ID.into())
        .await
        .unwrap();
```

**File:** rs/tests/boundary_nodes/rate_limit_canister_test.rs (L230-259)
```rust
    info!(
        &logger,
        "Step 7. Assert canister call fails (rejected), as authorized_principal is unset for the rate-limit canister",
    );

    assert!(result.unwrap_err().contains("reject"));

    info!(
        &logger,
        "Step 8. Upgrade rate-limit canister code via proposal, specifying authorized_principal in the payload",
    );

    let args = Encode!(&InitArg {
        registry_polling_period_secs: 5,
        authorized_principal: Some(full_access_principal),
    })
    .unwrap();

    // apply a no-impact WASM modification and reinstall the canister
    let new_wasm = modify_wasm_bytes(wasm.bytes().as_slice(), 42);

    upgrade_nns_canister_by_proposal(
        &rate_limit_canister,
        &Canister::new(&nns, GOVERNANCE_CANISTER_ID),
        &root,
        true,
        Wasm::from_bytes(new_wasm),
        Some(args),
    )
    .await;
```
