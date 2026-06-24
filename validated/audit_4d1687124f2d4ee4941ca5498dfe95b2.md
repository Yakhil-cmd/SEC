All three key files confirm the claims. Let me verify the `confidentiality_formatting.rs` behavior and the `inspect_message` scope before rendering a verdict.

The code confirms every factual assertion in the report:

1. **`random.rs` L8-14**: Seed is `time().to_le_bytes() ++ [42u8; 24]` — only 8 bytes vary, 24 bytes are constant `42`.
2. **`canister.rs` L34-68**: `inspect_message` only covers ingress update messages. `get_rule_by_id` is `#[query]` and is absent from both `UPDATE_METHODS` and `REPLICATED_QUERY_METHOD`, so it is never subject to `inspect_message` — any anonymous caller can invoke it as a regular query.
3. **`getter.rs` L224-246** + **`confidentiality_formatting.rs` L34-42**: For `RestrictedRead` callers, `RuleConfidentialityFormatter` only redacts `rule_raw` and `description` when `disclosed_at.is_none()`. Fields `rule_id`, `incident_id`, `added_in_version`, and `removed_in_version` are always returned.
4. **`getter.rs` L222**: `NotFound` is returned only when the UUID does not exist, providing a clean existence oracle.
5. **`add_config.rs` L115**: `generate_random_uuid()` is called for every new rule, drawing from the time-seeded `RNG`.

---

Audit Report

## Title
Predictable ChaCha20Rng Seed Enables UUID Prediction and Undisclosed Rule Metadata Enumeration — (`rs/boundary_node/rate_limits/canister/random.rs`)

## Summary
The `ChaCha20Rng` in the rate-limit canister is seeded with a value whose only variable component is the publicly observable IC consensus block time. Any observer can reconstruct the exact seed, predict all rule UUIDs generated after a canister install/upgrade, and call the unrestricted `get_rule_by_id` query to enumerate the existence and metadata (`incident_id`, `added_in_version`, `removed_in_version`) of undisclosed confidential rules.

## Finding Description
**Root cause — `random.rs` L8-14:**
```rust
thread_local! {
  static RNG: RefCell<ChaCha20Rng> = {
    let mut seed = [42; 32];
    seed[..8].copy_from_slice(&time().to_le_bytes());
    RefCell::new(ChaCha20Rng::from_seed(seed))
  };
}
```
The 32-byte seed is `time_ns_le ++ [42u8; 24]`. Only 8 bytes are non-constant; the remaining 24 bytes are always `42`. `ic_cdk::api::time()` returns the IC consensus block time in nanoseconds, which is publicly readable from the certified state tree. The `thread_local!` is initialized lazily on first access; after each canister install/upgrade the Wasm heap is reset, so the RNG is re-seeded on the first call to `generate_random_uuid()` — which occurs inside `add_config` at `add_config.rs` L115.

**Unrestricted query access — `canister.rs` L34-68, L123-133:**
`inspect_message` only intercepts ingress update messages. `get_rule_by_id` is annotated `#[query]` and is absent from both `UPDATE_METHODS` and `REPLICATED_QUERY_METHOD`, so it is never subject to `inspect_message`. Any anonymous caller can invoke it as a non-replicated query.

**Metadata leak — `getter.rs` L224-246, `confidentiality_formatting.rs` L34-42:**
`RuleConfidentialityFormatter::format` only redacts `rule_raw` and `description` when `disclosed_at.is_none()`. The fields `rule_id`, `incident_id`, `added_in_version`, and `removed_in_version` are always returned regardless of disclosure status. A `NotFound` error is returned only when the UUID does not exist (`getter.rs` L222), providing a clean existence oracle.

**Exploit flow:**
1. Attacker observes the IC block timestamp of the first `add_config` call after canister install/upgrade (available via certified state reads).
2. Reconstructs seed: `seed[..8] = block_time_ns.to_le_bytes(); seed[8..] = [42u8; 24]`.
3. Instantiates `ChaCha20Rng::from_seed(seed)` locally and replays `fill_bytes` calls to predict all subsequent rule UUIDs.
4. Calls `get_rule_by_id(predicted_uuid)` as an anonymous query to learn `incident_id`, `added_in_version`, `removed_in_version`, and rule existence for every undisclosed rule.

**Why existing checks fail:**
The `inspect_message` guard does not apply to query calls. The `RuleConfidentialityFormatter` redacts content fields but not structural metadata. There is no rate-limiting or authentication on `get_rule_by_id`.

## Impact Explanation
This is a **High** severity information disclosure vulnerability against boundary node infrastructure. An unprivileged attacker can enumerate the existence of confidential rate-limit rules and extract their `incident_id` (linking rules to undisclosed security incidents), `added_in_version`, and `removed_in_version` before public disclosure. This breaks the stated confidentiality invariant of the rate-limit canister and could allow adversaries to learn that a security incident is actively being mitigated — and correlate incident IDs across calls — before the incident is publicly disclosed. This matches: *High ($2,000–$10,000): Significant boundary/API infrastructure security impact with concrete user or protocol harm.*

## Likelihood Explanation
The attack requires no privileges, no key material, and no brute force. IC block times are publicly observable with nanosecond precision via certified state reads. The only prerequisite is monitoring the chain for the first `add_config` call after an install/upgrade, which is trivially detectable. The attack is fully deterministic and repeatable.

## Recommendation
Replace the time-seeded `thread_local!` initialization with a cryptographically unpredictable source. On the IC, use `ic_cdk::api::management_canister::main::raw_rand()` (an async call returning 32 bytes of threshold-BLS randomness) during `init`/`post_upgrade`, storing the resulting seed in a canister-held stable variable. Remove the `time()`-based lazy initialization entirely.

## Proof of Concept
```rust
// Deterministic replay — run against a local PocketIC instance
let block_time_ns: u64 = /* read from IC state tree at first add_config after install */;
let mut seed = [42u8; 32];
seed[..8].copy_from_slice(&block_time_ns.to_le_bytes());
let mut rng = ChaCha20Rng::from_seed(seed);

for _ in 0..N {
    let mut buf = [0u8; 16];
    rng.fill_bytes(&mut buf);
    let predicted_uuid = Uuid::from_slice(&buf).unwrap().to_string();
    // Anonymous query call:
    let response = canister.query("get_rule_by_id", predicted_uuid);
    // Assert: response is Ok (not NotFound) — rule exists
    // Assert: incident_id, added_in_version, removed_in_version are present
    // Assert: rule_raw and description are None (redacted but existence confirmed)
}
```
A PocketIC integration test can install the canister, call `add_config` N times, capture the block time, replay the RNG locally, and assert all N predicted UUIDs return `Ok` with non-null `incident_id` fields from an anonymous query caller.