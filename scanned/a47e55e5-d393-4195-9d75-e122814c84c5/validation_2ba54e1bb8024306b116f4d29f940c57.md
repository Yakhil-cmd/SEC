Let me examine the relevant files to verify the claims in this exploit question.

Let me check the `get_rule_by_id` handler and `inspect_message` guard to understand access control.

Let me check the confidentiality formatter to confirm exactly what fields are visible to unauthorized callers.

### Title
Predictable ChaCha20Rng Seed Enables Confidential Rule UUID Enumeration via Unauthenticated `get_rule_by_id` Query — (`rs/boundary_node/rate_limits/canister/random.rs`, `canister.rs`, `getter.rs`)

---

### Summary

The canister seeds its `ChaCha20Rng` with only 8 bytes of public IC time and 24 fixed bytes (`42`). Because `get_rule_by_id` is a `#[query]` method (bypassing `inspect_message` entirely), any anonymous caller can use it as an existence oracle. An attacker who reconstructs the seed can predict all generated rule UUIDs, call `get_rule_by_id` for each, and confirm existence of confidential (non-disclosed) rules — also leaking their `incident_id`.

---

### Finding Description

**Root cause 1 — Weak RNG seed** (`random.rs` lines 8–13):

```rust
thread_local! {
  static RNG: RefCell<ChaCha20Rng> = {
    let mut seed = [42; 32];
    seed[..8].copy_from_slice(&time().to_le_bytes());
    RefCell::new(ChaCha20Rng::from_seed(seed))
  };
}
``` [1](#0-0) 

The 32-byte seed has only 8 bytes of entropy (IC nanosecond time at first access), with the remaining 24 bytes hardcoded to `0x2A`. IC time is deterministic and publicly observable (recorded in the replicated state). The canister's initialization time can be retrieved from the IC's public state. This makes the full seed reconstructible.

**Root cause 2 — `get_rule_by_id` is an unauthenticated query** (`canister.rs` lines 123–133):

```rust
#[query]
fn get_rule_by_id(rule_id: RuleId) -> GetRuleByIdResponse {
    let caller_id = ic_cdk::api::caller();
    ...
    getter.get(&rule_id)
    ...
}
``` [2](#0-1) 

Query calls on the IC never pass through `inspect_message`. The `inspect_message` hook only covers update calls and the replicated query `get_config`. [3](#0-2) 

`get_rule_by_id` is not in `UPDATE_METHODS` or `REPLICATED_QUERY_METHOD`, so it is freely callable by any principal including anonymous.

**Root cause 3 — Existence oracle + `incident_id` leak** (`getter.rs` lines 215–246):

The `RuleGetter` returns `Err(NotFound)` for non-existent rules and `Ok(OutputRuleMetadata)` for existing ones. For unauthorized callers, `RuleConfidentialityFormatter` only redacts `rule_raw` and `description` — it does **not** redact `rule_id`, `incident_id`, `added_in_version`, or `removed_in_version`. [4](#0-3) [5](#0-4) 

This is confirmed by the test at `getter.rs` lines 433–446, which shows `incident_id` is visible to `RestrictedRead` callers. [6](#0-5) 

---

### Impact Explanation

An unprivileged attacker can:
1. Reconstruct the exact ChaCha20Rng seed from the public canister init time.
2. Count how many new rules have been generated (observable from public `get_config` responses showing rule counts per version).
3. Advance a local RNG by the same number of steps to predict future UUIDs.
4. Call `get_rule_by_id` as a query for each predicted UUID — no authentication required.
5. Distinguish `Ok(...)` (rule exists) from `Err(NotFound)` (rule doesn't exist).
6. For confirmed rules, read the `incident_id` from the response even though `rule_raw` and `description` are redacted.

This violates the invariant that confidential rule identifiers must not be enumerable by unauthorized callers, and leaks the `incident_id` of active security incidents before public disclosure.

---

### Likelihood Explanation

The attack is fully local-testable with no privileged access required. The only inputs needed are:
- The canister's init time (public IC state).
- The number of rules added per config version (public via `get_config`).

The seed search space is effectively zero once the init time is known (it is deterministic). Query calls are free and unauthenticated on the IC.

---

### Recommendation

1. **Fix the RNG seed**: Use `ic_cdk::api::management_canister::main::raw_rand()` (the IC's certified randomness beacon) to seed the RNG asynchronously during `init`/`post_upgrade`, rather than using `time()` with fixed padding.
2. **Restrict `get_rule_by_id` for non-disclosed rules**: Return `Err(NotFound)` (or a generic error) for unauthorized callers querying non-disclosed rules, rather than returning a redacted `Ok` response. This eliminates the existence oracle.
3. **Redact `incident_id`** in `RuleConfidentialityFormatter` for non-disclosed rules, consistent with the intent to keep confidential rule metadata hidden.

---

### Proof of Concept

```rust
// Attacker reconstructs seed from observed canister init time
let init_time_ns: u64 = /* observed from IC public state */;
let mut seed = [42u8; 32];
seed[..8].copy_from_slice(&init_time_ns.to_le_bytes());
let mut rng = ChaCha20Rng::from_seed(seed);

// Advance RNG by number of rules already generated (observable from get_config)
for _ in 0..already_generated_count {
    let mut buf = [0u8; 16];
    rng.fill_bytes(&mut buf);
}

// Predict next UUID
let mut buf = [0u8; 16];
rng.fill_bytes(&mut buf);
let predicted_uuid = Uuid::from_slice(&buf).unwrap();

// Call get_rule_by_id as anonymous query — no inspect_message check
// Ok(OutputRuleMetadata { incident_id: ..., rule_raw: None, ... }) → rule confirmed to exist
// Err(NotFound) → rule does not exist
``` [7](#0-6) [8](#0-7)

### Citations

**File:** rs/boundary_node/rate_limits/canister/random.rs (L8-13)
```rust
thread_local! {
  static RNG: RefCell<ChaCha20Rng> = {
    let mut seed = [42; 32];
    seed[..8].copy_from_slice(&time().to_le_bytes());
    RefCell::new(ChaCha20Rng::from_seed(seed))
  };
```

**File:** rs/boundary_node/rate_limits/canister/random.rs (L22-29)
```rust
pub fn custom_getrandom_bytes_impl(dest: &mut [u8]) -> Result<(), getrandom::Error> {
    RNG.with(|rng| {
        let mut rng = rng.borrow_mut();
        rng.fill_bytes(dest);
    });

    Ok(())
}
```

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L30-67)
```rust
const UPDATE_METHODS: [&str; 2] = ["add_config", "disclose_rules"];
const REPLICATED_QUERY_METHOD: &str = "get_config";

// Inspect the ingress messages in the pre-consensus phase and reject early, if the conditions are not met
#[inspect_message]
fn inspect_message() {
    // In order for this hook to succeed, accept_message() must be invoked.
    let caller_id: Principal = ic_cdk::api::caller();
    let called_method = ic_cdk::api::call::method_name();

    let (has_full_access, has_full_read_access) = with_canister_state(|state| {
        let authorized_principal = state.get_authorized_principal();
        (
            Some(caller_id) == authorized_principal,
            state.is_api_boundary_node_principal(&caller_id),
        )
    });

    if called_method == REPLICATED_QUERY_METHOD {
        if has_full_access || has_full_read_access {
            ic_cdk::api::call::accept_message();
        } else {
            ic_cdk::api::trap(
                "message_inspection_failed: method call is prohibited in the current context",
            );
        }
    } else if UPDATE_METHODS.contains(&called_method.as_str()) {
        if has_full_access {
            ic_cdk::api::call::accept_message();
        } else {
            ic_cdk::api::trap("message_inspection_failed: unauthorized caller");
        }
    } else {
        // All others calls are rejected
        ic_cdk::api::trap(
            "message_inspection_failed: method call is prohibited in the current context",
        );
    }
```

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L123-133)
```rust
#[query]
fn get_rule_by_id(rule_id: RuleId) -> GetRuleByIdResponse {
    let caller_id = ic_cdk::api::caller();
    let response = with_canister_state(|state| {
        let access_resolver = AccessLevelResolver::new(caller_id, state.clone());
        let formatter = RuleConfidentialityFormatter;
        let getter = RuleGetter::new(state, formatter, access_resolver);
        getter.get(&rule_id)
    })?;
    Ok(response)
}
```

**File:** rs/boundary_node/rate_limits/canister/confidentiality_formatting.rs (L31-42)
```rust
impl ConfidentialityFormatting for RuleConfidentialityFormatter {
    type Input = OutputRuleMetadata;

    fn format(&self, rule: OutputRuleMetadata) -> OutputRuleMetadata {
        let mut rule = rule;
        // Redact (hide) fields of non-disclosed rule
        if rule.disclosed_at.is_none() {
            rule.description = None;
            rule.rule_raw = None;
        }
        rule
    }
```

**File:** rs/boundary_node/rate_limits/canister/getter.rs (L219-246)
```rust
        let stored_rule = self
            .canister_api
            .get_rule(&rule_id)
            .ok_or_else(|| GetEntityError::NotFound(rule_id.0.to_string()))?;

        let output_rule = OutputRuleMetadata {
            id: rule_id,
            incident_id: stored_rule.incident_id,
            rule_raw: Some(stored_rule.rule_raw),
            description: Some(stored_rule.description),
            disclosed_at: stored_rule.disclosed_at,
            added_in_version: stored_rule.added_in_version,
            removed_in_version: stored_rule.removed_in_version,
        };

        let is_authorized_viewer = self.access_resolver.get_access_level()
            == AccessLevel::FullAccess
            || self.access_resolver.get_access_level() == AccessLevel::FullRead;

        if is_authorized_viewer {
            return Ok(output_rule.into());
        }

        // Hide non-disclosed rules from unauthorized viewers.
        let output_rule = self.formatter.format(output_rule);

        Ok(output_rule.into())
    }
```

**File:** rs/boundary_node/rate_limits/canister/getter.rs (L433-446)
```rust
        let response = getter_unauthorized.get(&rule_id.0.to_string()).unwrap();
        // rule fields are hidden
        assert_eq!(
            response,
            api::OutputRuleMetadata {
                rule_id: rule_id.0.to_string(),
                incident_id: incident_id.0.to_string(),
                rule_raw: None,
                description: None,
                disclosed_at: None,
                added_in_version: 1,
                removed_in_version: Some(3),
            }
        );
```

**File:** rs/boundary_node/rate_limits/canister/add_config.rs (L186-193)
```rust
fn generate_random_uuid() -> Result<Uuid, anyhow::Error> {
    let mut buf = [0_u8; 16];
    getrandom::getrandom(&mut buf)
        .map_err(|e| anyhow::anyhow!(e))
        .context("Failed to generate random bytes")?;
    let uuid = Uuid::from_slice(&buf).context("Failed to create UUID from bytes")?;
    Ok(uuid)
}
```
