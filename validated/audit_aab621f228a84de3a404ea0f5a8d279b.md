Audit Report

## Title
Predictable ChaCha20 RNG Seed Combined with `get_rule_by_id` Metadata Leakage Enables Undisclosed Rule Enumeration — (`rs/boundary_node/rate_limits/canister/random.rs`, `add_config.rs`, `getter.rs`)

## Summary

The rate-limit canister seeds its ChaCha20 RNG with only 8 bytes of entropy (IC time in nanoseconds), with the remaining 24 bytes hardcoded to `42`. Because IC time is publicly observable and the RNG is deterministic, any observer can reconstruct the exact RNG state and predict every UUID assigned as a rule ID. The `get_rule_by_id` query is accessible to any anonymous caller and returns `Ok` with `incident_id`, `added_in_version`, and `removed_in_version` for undisclosed rules, rather than an indistinguishable `Err(NotFound)`. Combined, these two weaknesses allow an unprivileged attacker to enumerate undisclosed rules — including removed ones not visible in `get_config` — and extract their security-sensitive metadata before official disclosure.

## Finding Description

**Root cause 1 — Weak RNG seed** (`random.rs` lines 8–14):

```rust
thread_local! {
  static RNG: RefCell<ChaCha20Rng> = {
    let mut seed = [42; 32];
    seed[..8].copy_from_slice(&time().to_le_bytes());
    RefCell::new(ChaCha20Rng::from_seed(seed))
  };
}
```

The effective entropy is 64 bits — the IC nanosecond timestamp at canister init. The remaining 24 bytes of the seed are the constant `42`. The IC canister creation time is publicly readable from certified canister history, so the full seed is reconstructible by any observer.

**Root cause 2 — UUID generation flows from this RNG** (`add_config.rs` lines 186–193):

`generate_random_uuid()` calls `getrandom::getrandom()`, which on `wasm32-unknown-unknown` is routed through `custom_getrandom_bytes_impl` (`random.rs` lines 22–29), draining bytes from the same `thread_local! RNG`. Every new rule ID is the next 16 bytes from this single, predictable stream.

**Root cause 3 — `get_rule_by_id` is an unrestricted query** (`canister.rs` lines 123–133):

`get_rule_by_id` is annotated `#[query]`. The `inspect_message` hook (`canister.rs` lines 34–68) only intercepts update calls and the `get_config` replicated query. Query calls bypass `inspect_message` entirely on the IC, so any anonymous principal can call `get_rule_by_id` without restriction.

**Root cause 4 — `RuleConfidentialityFormatter` does not suppress all metadata** (`confidentiality_formatting.rs` lines 31–42):

```rust
fn format(&self, rule: OutputRuleMetadata) -> OutputRuleMetadata {
    let mut rule = rule;
    if rule.disclosed_at.is_none() {
        rule.description = None;
        rule.rule_raw = None;
    }
    rule
}
```

`incident_id`, `added_in_version`, and `removed_in_version` are passed through unchanged. `RuleGetter::get()` (`getter.rs` lines 219–245) returns `Err(NotFound)` only when the rule does not exist in storage; if the rule exists but is undisclosed, it returns `Ok(output_rule.into())` after applying the formatter. The existing test (`getter.rs` lines 433–446) explicitly asserts this behavior: an `AccessLevel::RestrictedRead` caller receives a successful `Ok` response containing `rule_id`, `incident_id`, `added_in_version`, and `removed_in_version` for an undisclosed rule.

**Exploit path:**

1. Read the canister's deployment timestamp from IC certified state (public, no privileges required).
2. Reconstruct the ChaCha20 seed: `[T0..T7, 42×24]`.
3. Pre-compute the sequence of rule UUIDs that will be assigned.
4. For each predicted UUID, call `get_rule_by_id(uuid)` as an anonymous query.
5. An `Ok` response with nulled `rule_raw`/`description` confirms the rule exists but is undisclosed; `Err(NotFound)` confirms it does not exist.
6. Extract `incident_id`, `added_in_version`, and `removed_in_version` from the `Ok` response — before official disclosure.

This is particularly impactful for rules that have been removed from the current config (and thus are invisible in `get_config`) but have not yet been disclosed: UUID prediction is the only way to discover their IDs, and `get_rule_by_id` then leaks their metadata.

## Impact Explanation

This breaks the confidentiality invariant of the boundary node rate-limit canister: undisclosed rule metadata — specifically the `incident_id` linking a rule to a security incident, and its version lifecycle — is reachable by any anonymous caller before official disclosure. The boundary node rate-limit canister is explicitly listed as an in-scope target. The impact is a significant boundary/API security impact: premature disclosure of security incident linkage data could allow an attacker to understand what vulnerabilities are being mitigated before public disclosure, undermining the purpose of the confidentiality mechanism. This maps to the **Medium ($200–$2,000)** impact tier: the attack requires meaningful technical steps (reconstructing RNG state, enumerating UUIDs) but no privileges, and the leaked information is metadata rather than full rule content.

## Likelihood Explanation

- The canister init time is a single 64-bit value with no additional secret; it is not guessed but read from public IC certified state.
- The attack requires zero privileges, no governance majority, no key material, and no network-level position.
- The ChaCha20 RNG is fully deterministic given the seed; UUID prediction is exact, not probabilistic.
- The only precondition is the canister's deployment timestamp, which is trivially observable.
- The attack is fully reproducible in a PocketIC or local replica environment.

## Recommendation

**Fix 1 — Replace the weak seed**: Use `ic_cdk::api::management_canister::main::raw_rand()` during `init`/`post_upgrade` to obtain 32 bytes of certified randomness from the threshold BLS beacon. Defer UUID generation until the async call completes.

**Fix 2 — Suppress all metadata for undisclosed rules from `RestrictedRead` callers**: `RuleGetter::get()` should return `Err(NotFound)` (indistinguishable from a non-existent rule) when the rule is undisclosed and the caller's access level is `RestrictedRead`, rather than returning a successful response with partial metadata. This closes the oracle that distinguishes "exists but undisclosed" from "does not exist."

## Proof of Concept

```rust
use rand_chacha::ChaCha20Rng;
use rand_chacha::rand_core::{RngCore, SeedableRng};
use uuid::Uuid;

fn predict_rule_ids(init_time_ns: u64, count: usize) -> Vec<String> {
    let mut seed = [42u8; 32];
    seed[..8].copy_from_slice(&init_time_ns.to_le_bytes());
    let mut rng = ChaCha20Rng::from_seed(seed);
    (0..count).map(|_| {
        let mut buf = [0u8; 16];
        rng.fill_bytes(&mut buf);
        Uuid::from_slice(&buf).unwrap().to_string()
    }).collect()
}

// Steps:
// 1. Read canister init time from IC certified state (public).
// 2. Call predict_rule_ids(init_time, 1000).
// 3. For each predicted UUID, call get_rule_by_id(uuid) as an anonymous query.
// 4. Assert Ok(...) responses for rules that exist but are not yet disclosed
//    (including removed rules not visible in get_config).
// 5. Observe incident_id, added_in_version, removed_in_version in the response.
```