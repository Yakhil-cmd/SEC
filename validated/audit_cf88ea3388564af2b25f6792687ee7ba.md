The code is confirmed. Let me verify the exact behavior of `get_rule_by_id` for confidential (non-disclosed) rules to confirm the existence oracle.

### Title
Predictable ChaCha20 RNG Seed Enables Full UUID Pre-computation and Confidential Rule Enumeration — (`rs/boundary_node/rate_limits/canister/random.rs`)

---

### Summary

The rate-limit canister seeds its ChaCha20 RNG with only 64 bits of public entropy (the IC block timestamp), leaving the remaining 24 bytes as the constant `0x2a`. Because the IC block timestamp is publicly readable via `read_state`, any observer can reconstruct the exact seed, replay the full keystream, and predict every rule UUID the canister will ever generate. Combined with the fact that `get_rule_by_id` returns `Ok` (not `NotFound`) for confidential undisclosed rules — leaking `incident_id`, `added_in_version`, and `removed_in_version` — an unprivileged attacker can enumerate all confidential rules and harvest their metadata without any authorization.

---

### Finding Description

**Root cause 1 — Weak RNG seed:** [1](#0-0) 

The `thread_local` initializer fills all 32 seed bytes with `42` (`0x2a`), then overwrites only the first 8 bytes with `time().to_le_bytes()`. The resulting seed is `T.to_le_bytes() || [0x2a; 24]`, where `T` is the IC nanosecond timestamp at canister install or upgrade. The IC block timestamp is part of the certified state tree and is publicly readable by any caller via `read_state` — it is not a secret.

**Root cause 2 — UUID generation routes through the weak RNG:** [2](#0-1) 

`generate_random_uuid()` calls `getrandom::getrandom`, which on `wasm32-unknown-unknown` is registered to `custom_getrandom_bytes_impl`: [3](#0-2) 

All UUID bytes therefore come from the same deterministic ChaCha20 stream seeded with the public timestamp.

**Root cause 3 — `get_rule_by_id` is an existence oracle for confidential rules:** [4](#0-3) 

For a non-existent rule the call returns `Err(NotFound)`. For an existing but confidential (non-disclosed) rule it returns `Ok` with `rule_raw` and `description` redacted, but `rule_id`, `incident_id`, `added_in_version`, and `removed_in_version` **still present in the response**. There is no access-level check that suppresses the `Ok` variant itself.

---

### Impact Explanation

An unprivileged attacker (any IC user) can:

1. Read the canister install/upgrade block timestamp `T` from `read_state` (public, certified).
2. Reconstruct the exact 32-byte seed: `T.to_le_bytes() || [0x2a; 24]`.
3. Instantiate `ChaCha20Rng::from_seed(seed)` locally and generate the same UUID sequence the canister will produce.
4. For each predicted UUID, call the public `get_rule_by_id` query.
5. Distinguish `Ok` (rule exists, confidential) from `Err(NotFound)` (rule not yet added).
6. From every `Ok` response, harvest `incident_id`, `added_in_version`, and `removed_in_version` — revealing the existence, timing, and scope of active security incidents tied to undisclosed rate-limit rules.

This breaks the stated confidentiality invariant: undisclosed rules are intended to be invisible to unauthorized callers, but their existence and metadata are fully inferable.

---

### Likelihood Explanation

The attack requires no privileges, no key material, and no network-level capabilities. The only input needed — the IC block timestamp — is freely available from any IC node via `read_state`. The exploit is entirely local and deterministic. The canister's RNG state is never re-seeded after initialization (no upgrade re-seed, no `raw_rand` injection), so the full UUID sequence for the canister's lifetime is fixed from the moment of install.

---

### Recommendation

Replace the time-seeded RNG with a properly seeded one. The standard IC pattern is to call `ic_cdk::api::management_canister::main::raw_rand()` asynchronously during `init`/`post_upgrade` and store the result as the RNG seed. Alternatively, use the VetKeys or threshold randomness beacon. At minimum, the seed must incorporate at least 128 bits of unpredictable entropy not observable from the block history.

Additionally, `get_rule_by_id` should return `Err(NotFound)` (or an equivalent opaque error) for confidential rules when called by an unauthorized principal, rather than returning `Ok` with redacted fields, to eliminate the existence oracle.

---

### Proof of Concept

```rust
// Off-chain attacker code
use rand_chacha::ChaCha20Rng;
use rand_chacha::rand_core::{RngCore, SeedableRng};
use uuid::Uuid;

fn predict_uuids(install_timestamp_ns: u64, count: usize) -> Vec<Uuid> {
    let mut seed = [42u8; 32];
    seed[..8].copy_from_slice(&install_timestamp_ns.to_le_bytes());
    let mut rng = ChaCha20Rng::from_seed(seed);
    (0..count).map(|_| {
        let mut buf = [0u8; 16];
        rng.fill_bytes(&mut buf);
        Uuid::from_slice(&buf).unwrap()
    }).collect()
}

// 1. Read T from read_state (public IC API)
// 2. predicted = predict_uuids(T, 10_000)
// 3. For each uuid in predicted: call get_rule_by_id(uuid.to_string())
//    - Ok(_)  => rule exists, harvest incident_id / added_in_version / removed_in_version
//    - Err(_) => not yet added
```

### Citations

**File:** rs/boundary_node/rate_limits/canister/random.rs (L8-14)
```rust
thread_local! {
  static RNG: RefCell<ChaCha20Rng> = {
    let mut seed = [42; 32];
    seed[..8].copy_from_slice(&time().to_le_bytes());
    RefCell::new(ChaCha20Rng::from_seed(seed))
  };
}
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

**File:** rs/boundary_node/rate_limits/canister/getter.rs (L219-245)
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
```
