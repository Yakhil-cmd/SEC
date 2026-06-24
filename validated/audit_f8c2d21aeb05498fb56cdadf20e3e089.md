Looking at the actual production code to validate each step of the claimed exploit path.

Audit Report

## Title
Predictable ChaCha20 RNG Seed Enables UUID Enumeration and Undisclosed Rule Metadata Leakage — (`rs/boundary_node/rate_limits/canister/random.rs`, `add_config.rs`, `getter.rs`)

## Summary

The rate-limit canister seeds its ChaCha20 RNG with only 8 bytes of entropy (IC time in nanoseconds at first use), with the remaining 24 bytes hardcoded to `42`. Because IC time is publicly observable and the RNG is deterministic, any observer can reconstruct the exact RNG state, predict every UUID assigned as a rule ID, and then call the publicly accessible `get_rule_by_id` query to confirm rule existence and extract undisclosed rule metadata (`incident_id`, `added_in_version`, `removed_in_version`) before official disclosure. The `RuleConfidentialityFormatter` only suppresses `rule_raw` and `description` for undisclosed rules, leaving the remaining metadata fields fully exposed in the `Ok` response.

## Finding Description

**Root cause 1 — Weak RNG seed**

`random.rs` lines 8–14 initialize the `thread_local! RNG` with a 32-byte seed where only the first 8 bytes come from `ic_cdk::api::time()` and the remaining 24 bytes are the constant `42`:

```rust
let mut seed = [42; 32];
seed[..8].copy_from_slice(&time().to_le_bytes());
RefCell::new(ChaCha20Rng::from_seed(seed))
```

The effective entropy is a single 64-bit IC timestamp. On the IC, the wasm instance persists across calls, so this `thread_local!` is initialized exactly once per canister lifetime (reset on upgrade), at the moment of first access to the RNG — which occurs during the first `add_config` update call. The IC time of any update call is publicly observable from IC certified state and canister history.

**Root cause 2 — UUID generation flows entirely from this RNG**

`add_config.rs` lines 186–193 call `getrandom::getrandom()` to generate each new rule UUID. On `wasm32-unknown-unknown`, this is routed through the registered custom handler `custom_getrandom_bytes_impl` (`random.rs` lines 22–29), which drains bytes from the same `thread_local! RNG`. Every rule UUID is therefore the next 16 bytes from this single, predictable stream.

**Root cause 3 — `get_rule_by_id` is an unrestricted `#[query]`**

`canister.rs` lines 123–133 expose `get_rule_by_id` as a `#[query]` method. The `inspect_message` hook (`canister.rs` lines 34–68) only intercepts ingress (update) calls; it covers `get_config` (replicated query) and `add_config`/`disclose_rules` (update methods). Non-replicated query calls bypass `inspect_message` entirely on the IC. Any anonymous principal can call `get_rule_by_id` without restriction.

**Root cause 4 — Insufficient confidentiality filtering**

`RuleGetter::get()` (`getter.rs` lines 219–246) returns `Err(NotFound)` only when the rule does not exist in storage. If the rule exists but is undisclosed, it returns `Ok(OutputRuleMetadata{...})` after applying `RuleConfidentialityFormatter`. The formatter (`confidentiality_formatting.rs` lines 31–42) only nulls `rule_raw` and `description` when `disclosed_at.is_none()`. It does **not** suppress `rule_id`, `incident_id`, `added_in_version`, or `removed_in_version`. This is confirmed by the unit test at `getter.rs` lines 433–446, which asserts that an `AccessLevel::RestrictedRead` caller receives a successful `Ok` response containing `incident_id`, `added_in_version`, and `removed_in_version` for an undisclosed rule.

**Exploit path**

1. Read the IC time of the first `add_config` call from public IC certified state or canister history.
2. Reconstruct the exact ChaCha20 RNG state: `seed = [T0..T7, 42×24]`.
3. Pre-compute the full sequence of rule UUIDs that will be assigned.
4. Call `get_rule_by_id(uuid)` as an anonymous query for each predicted UUID.
5. Distinguish `Ok(...)` (rule exists, possibly undisclosed) from `Err(NotFound)` (rule does not exist).
6. Extract `incident_id`, `added_in_version`, and `removed_in_version` from every `Ok` response for undisclosed rules — before official disclosure.

**Existing guards reviewed and found insufficient**

- `inspect_message` does not apply to `#[query]` calls; it cannot block anonymous access to `get_rule_by_id`.
- `RuleConfidentialityFormatter` suppresses only `rule_raw` and `description`; it does not suppress `incident_id` or version metadata, and does not return `Err(NotFound)` to make undisclosed rules indistinguishable from non-existent ones.
- The UUID space (128 bits) is not enumerable by brute force, but UUID prediction reduces the search space to a single 64-bit IC timestamp, which is publicly observable.

## Impact Explanation

The rate-limit canister is an in-scope boundary node infrastructure component. Its confidentiality model requires that undisclosed rule metadata (including the `incident_id` linking a rule to a security incident) must not be reachable by unprivileged callers before official disclosure. This vulnerability breaks that invariant: any anonymous caller can learn the existence of undisclosed security incidents, their associated `incident_id` UUIDs, and the config version ranges in which they were active — before the operator discloses them. This constitutes a **significant boundary/API security impact with concrete harm**: premature leakage of security incident metadata undermines the operational security of the boundary node rate-limit system and could allow adversaries to correlate undisclosed mitigations with ongoing attacks.

This matches the allowed High impact: *Significant boundary/API infrastructure security impact with concrete user or protocol harm* ($2,000–$10,000).

## Likelihood Explanation

- Zero privileges required; any anonymous principal can call `get_rule_by_id` as a query.
- The only precondition is the IC time of the first `add_config` call, which is trivially readable from public IC certified state or canister history — no guessing required.
- The attack is fully local-testable in a PocketIC environment.
- No network-level attack, social engineering, or third-party compromise is required.
- The attack is repeatable after each canister upgrade (the RNG reseeds with the new first-`add_config` time).

## Recommendation

**Fix 1 — Replace weak RNG seeding**: Use `ic_cdk::api::management_canister::main::raw_rand()` during `init`/`post_upgrade` to obtain 32 bytes of certified randomness from the threshold BLS beacon. Store the result in stable memory and use it as the ChaCha20 seed. Defer UUID generation until the async `raw_rand()` call completes.

**Fix 2 — Fix `get_rule_by_id` confidentiality**: For `AccessLevel::RestrictedRead` callers, return `Err(GetEntityError::NotFound)` for undisclosed rules, making them indistinguishable from non-existent rules. Do not return a successful `Ok` response with partial metadata for undisclosed rules to unprivileged callers.

## Proof of Concept

```rust
// Deterministic replay — runnable in pocket-ic or local replica
use rand_chacha::ChaCha20Rng;
use rand_chacha::rand_core::{RngCore, SeedableRng};
use uuid::Uuid;

fn predict_rule_ids(first_add_config_time_ns: u64, count: usize) -> Vec<String> {
    let mut seed = [42u8; 32];
    seed[..8].copy_from_slice(&first_add_config_time_ns.to_le_bytes());
    let mut rng = ChaCha20Rng::from_seed(seed);
    (0..count).map(|_| {
        let mut buf = [0u8; 16];
        rng.fill_bytes(&mut buf);
        Uuid::from_slice(&buf).unwrap().to_string()
    }).collect()
}

// Steps:
// 1. Deploy canister; call add_config as authorized principal.
// 2. Read the IC time of that update call from certified state (public).
// 3. Call predict_rule_ids(observed_time, 100).
// 4. For each predicted UUID, call get_rule_by_id(uuid) as Principal::anonymous().
// 5. Assert Ok(...) responses for rules that exist but are not yet disclosed.
// 6. Observe incident_id, added_in_version, removed_in_version in the response —
//    fields that the confidentiality formatter does not suppress.
```