All three code claims are verified. Let me confirm the key details before writing the report.

All three code claims are confirmed by the actual source. Here is the audit report:

---

Audit Report

## Title
Predictable ChaCha20Rng Seed Combined with Unauthenticated Query and Existence Oracle Enables Confidential Rule ID Enumeration — (`rs/boundary_node/rate_limits/canister/random.rs`, `canister.rs`, `getter.rs`)

## Summary
The canister's `ChaCha20Rng` is seeded with only 8 bytes of publicly observable IC block time and 24 constant bytes (`0x2A`), making the UUID sequence fully predictable. `get_rule_by_id` is a `#[query]` method that any anonymous caller can invoke because `inspect_message` only fires for ingress/update calls. `RuleGetter::get` returns a structurally distinct `Ok(redacted)` for existing confidential rules versus `Err(NotFound)` for absent ones, creating a rule-existence oracle. Together these allow an unprivileged attacker to enumerate the IDs of confidential, non-disclosed rate-limit rules protecting active security incidents.

## Finding Description

**Step 1 — Weak, predictable RNG seed.**

`random.rs` initializes the thread-local `ChaCha20Rng` with a 32-byte seed where only the first 8 bytes come from `ic_cdk::api::time()` and the remaining 24 bytes are the constant `0x2A`: [1](#0-0) 

This `thread_local!` initializer runs lazily on the first call to `custom_getrandom_bytes_impl`, which is invoked via `getrandom::getrandom()` inside `generate_random_uuid()`: [2](#0-1) 

`generate_random_uuid()` is called at line 115 of `add_config.rs` whenever a new rule is introduced: [3](#0-2) 

The IC `time()` returns the block timestamp in nanoseconds, which is part of the certified state and publicly observable. The first call to `generate_random_uuid` occurs during the first `add_config` invocation that introduces a new rule; the block timestamp of that call is observable on-chain. With the seed fully determined, an attacker can instantiate an identical `ChaCha20Rng` locally and reproduce the entire UUID sequence.

**Step 2 — `get_rule_by_id` is freely accessible as a query.**

`get_rule_by_id` is declared `#[query]` with no access control: [4](#0-3) 

The `inspect_message` hook covers only `UPDATE_METHODS` (`["add_config", "disclose_rules"]`) and `REPLICATED_QUERY_METHOD` (`"get_config"`): [5](#0-4) 

At the IC protocol level, `inspect_message` fires only for ingress (update) calls. Query calls bypass it entirely. `get_rule_by_id` and `get_rules_by_incident_id` are absent from both constants and are never mentioned in the hook, so any anonymous principal can call them freely.

**Step 3 — Response leaks rule existence.**

In `RuleGetter::get`, the storage lookup occurs before any access-level check: [6](#0-5) 

If the rule does not exist, the function returns `Err(NotFound)`. If it exists but the caller is unauthorized, execution continues and returns `Ok(redacted_rule)`: [7](#0-6) 

`RuleConfidentialityFormatter::format` only nulls out `rule_raw` and `description`; it never converts the response to an error: [8](#0-7) 

The two response variants (`Ok` with nulled fields vs. `Err(NotFound)`) are structurally distinct and trivially distinguishable by any caller.

**Exploit chain:**
1. Observe the IC block timestamp of the first `add_config` call (public, certified state).
2. Reconstruct the seed: `seed[0..8] = observed_time.to_le_bytes(); seed[8..32] = [42u8; 24]`.
3. Instantiate `ChaCha20Rng::from_seed(seed)` locally and generate the UUID sequence.
4. For each predicted UUID, call `get_rule_by_id(uuid)` as an anonymous query.
5. `Ok(...)` → rule exists and is confidential. `Err(NotFound)` → UUID not assigned.

## Impact Explanation
An unprivileged attacker can confirm the existence of confidential, non-disclosed rate-limit rules — rules that protect against active security incidents at boundary nodes. The `Ok` response also leaks `incident_id`, `added_in_version`, and `removed_in_version`, revealing that an incident is being actively mitigated before public disclosure. This is a concrete information disclosure impact against the boundary node infrastructure, matching: **High ($2,000–$10,000) — Significant boundary/API infrastructure security impact with concrete user or protocol harm.**

## Likelihood Explanation
The attack requires no privileged access, no key material, and no social engineering. All required inputs (IC block timestamp, anonymous query endpoint) are publicly accessible on mainnet. The seed reconstruction is deterministic — the IC block timestamp is exact, so no brute-force is needed in the common case. If the exact nanosecond is uncertain, the offline search space is bounded by the block duration (~10⁹ values), which is parallelizable. The query endpoint is reachable by any HTTP client. Likelihood is **high**.

## Recommendation
1. **Fix the RNG seed**: Replace the time-based seed with `ic_cdk::api::management_canister::main::raw_rand()` called during `init`/`post_upgrade`. Store the 32 bytes of verifiable randomness in stable memory and use them as the `ChaCha20Rng` seed. Remove the `[42; 32]` constant seed entirely from `random.rs`.

2. **Fix the existence oracle**: In `RuleGetter::get` (`getter.rs`), move the access-level check before returning any data. For unauthorized callers, return `Err(NotFound)` (or `Err(Unauthorized)`) for confidential rules rather than `Ok(redacted)`. This prevents distinguishing "exists but hidden" from "does not exist."

3. **Alternatively, gate `get_rule_by_id` on access level**: Return `Err(Unauthorized)` or `Err(NotFound)` for any caller without `FullAccess` or `FullRead` when the rule is not disclosed.

## Proof of Concept

```rust
use rand_chacha::ChaCha20Rng;
use rand_chacha::rand_core::{RngCore, SeedableRng};
use uuid::Uuid;

fn predict_uuids(canister_first_add_config_block_time_ns: u64, count: usize) -> Vec<Uuid> {
    let mut seed = [42u8; 32];
    seed[..8].copy_from_slice(&canister_first_add_config_block_time_ns.to_le_bytes());
    let mut rng = ChaCha20Rng::from_seed(seed);
    (0..count).map(|_| {
        let mut buf = [0u8; 16];
        rng.fill_bytes(&mut buf);
        Uuid::from_slice(&buf).unwrap()
    }).collect()
}

// For each uuid in predict_uuids(observed_block_time, 1000):
//   call get_rule_by_id(uuid.to_string()) as an anonymous query
//   Ok(_)  => confidential rule confirmed to exist; incident_id and version metadata also leaked
//   Err(_) => UUID not assigned
```

The `observed_block_time` is read from the IC certified state (block timestamp of the first `add_config` transaction that introduced a new rule). A PocketIC integration test can reproduce this deterministically by controlling the mock time passed to `add_config` and verifying that `get_rule_by_id` returns `Ok` for the predicted UUID and `Err(NotFound)` for an unpredicted one.

### Citations

**File:** rs/boundary_node/rate_limits/canister/random.rs (L9-13)
```rust
  static RNG: RefCell<ChaCha20Rng> = {
    let mut seed = [42; 32];
    seed[..8].copy_from_slice(&time().to_le_bytes());
    RefCell::new(ChaCha20Rng::from_seed(seed))
  };
```

**File:** rs/boundary_node/rate_limits/canister/add_config.rs (L112-116)
```rust
            let rule_id = if let Some(rule_idx) = existing_rule_idx {
                current_config.rule_ids[rule_idx]
            } else {
                let rule_id = RuleId(generate_random_uuid()?);
                // If the generated UUID already exists, return the error (practically this should never happen).
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

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L30-31)
```rust
const UPDATE_METHODS: [&str; 2] = ["add_config", "disclose_rules"];
const REPLICATED_QUERY_METHOD: &str = "get_config";
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

**File:** rs/boundary_node/rate_limits/canister/getter.rs (L219-222)
```rust
        let stored_rule = self
            .canister_api
            .get_rule(&rule_id)
            .ok_or_else(|| GetEntityError::NotFound(rule_id.0.to_string()))?;
```

**File:** rs/boundary_node/rate_limits/canister/getter.rs (L234-245)
```rust
        let is_authorized_viewer = self.access_resolver.get_access_level()
            == AccessLevel::FullAccess
            || self.access_resolver.get_access_level() == AccessLevel::FullRead;

        if is_authorized_viewer {
            return Ok(output_rule.into());
        }

        // Hide non-disclosed rules from unauthorized viewers.
        let output_rule = self.formatter.format(output_rule);

        Ok(output_rule.into())
```

**File:** rs/boundary_node/rate_limits/canister/confidentiality_formatting.rs (L34-41)
```rust
    fn format(&self, rule: OutputRuleMetadata) -> OutputRuleMetadata {
        let mut rule = rule;
        // Redact (hide) fields of non-disclosed rule
        if rule.disclosed_at.is_none() {
            rule.description = None;
            rule.rule_raw = None;
        }
        rule
```
