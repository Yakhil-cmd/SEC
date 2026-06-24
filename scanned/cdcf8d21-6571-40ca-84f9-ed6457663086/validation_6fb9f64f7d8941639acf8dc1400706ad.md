### Title
Timestamp-Seeded PRNG with Hardcoded Constant Produces Fully Predictable UUIDs - (File: `rs/boundary_node/rate_limits/canister/random.rs`)

---

### Summary

The rate-limit canister's custom `getrandom` implementation seeds a `ChaCha20Rng` with a 32-byte array in which **24 bytes are the hardcoded constant `42`** and only 8 bytes come from `ic_cdk::api::time()` (the IC consensus timestamp). Because the IC consensus time is publicly observable on-chain and the remaining 24 bytes are a fixed constant, every UUID generated for rate-limit rule IDs (`RuleId`) is fully predictable by any external observer.

---

### Finding Description

In `rs/boundary_node/rate_limits/canister/random.rs`, the thread-local RNG is initialized as follows:

```rust
thread_local! {
  static RNG: RefCell<ChaCha20Rng> = {
    let mut seed = [42; 32];
    seed[..8].copy_from_slice(&time().to_le_bytes());
    RefCell::new(ChaCha20Rng::from_seed(seed))
  };
}
``` [1](#0-0) 

Only 8 bytes of the 32-byte seed carry any entropy — the IC consensus timestamp in nanoseconds returned by `ic_cdk::api::time()`. The remaining 24 bytes are the constant `42`. This RNG is registered as the canister's sole `getrandom` backend:

```rust
getrandom::register_custom_getrandom!(custom_getrandom_bytes_impl);
``` [2](#0-1) 

The custom implementation feeds all `getrandom::getrandom()` calls from this RNG: [3](#0-2) 

`generate_random_uuid()` in `add_config.rs` calls `getrandom::getrandom()` to fill a 16-byte buffer and constructs a `RuleId` UUID from it:

```rust
fn generate_random_uuid() -> Result<Uuid, anyhow::Error> {
    let mut buf = [0_u8; 16];
    getrandom::getrandom(&mut buf)
        ...
    let uuid = Uuid::from_slice(&buf)...;
    Ok(uuid)
}
``` [4](#0-3) 

This UUID is used as the `RuleId` for every new rate-limit rule added to the canister: [5](#0-4) 

The effective seed entropy is therefore **64 bits** (the timestamp), not 256 bits, and the remaining 192 bits are a known constant. The entire future UUID sequence is deterministically derivable from the IC consensus time at which the thread-local is first initialized (i.e., the first `add_config` call).

---

### Impact Explanation

Any external observer who records the IC consensus time `T` at which the first `add_config` call is made can:

1. Reconstruct the seed: `[T_bytes (8 bytes) || 42 (24 bytes)]`
2. Initialize `ChaCha20Rng::from_seed(seed)` locally
3. Advance the RNG to predict every `RuleId` UUID the canister will ever generate

This breaks any security property that depends on `RuleId` opacity. More concretely, the severely reduced entropy (64 effective bits out of 256) makes the seed space small enough for offline brute-force enumeration even without knowing the exact initialization time — an attacker can enumerate all plausible nanosecond timestamps within a reasonable window and identify the correct one by matching a single observed `RuleId`.

---

### Likelihood Explanation

The IC consensus time is publicly observable: it is embedded in certified block metadata and the certified state tree, accessible to any boundary/API user without any privileged access. The thread-local is initialized on the first invocation of `custom_getrandom_bytes_impl`, which occurs during the first `add_config` call — an on-chain event whose timestamp is permanently recorded. No special role or key material is required to observe the seed.

---

### Recommendation

Replace the timestamp-based seed with randomness obtained from the IC's threshold BLS signature scheme via `ic_cdk::api::management_canister::main::raw_rand()`. Because `raw_rand` is asynchronous, the canister should seed the RNG during `canister_init` or `canister_post_upgrade` by awaiting `raw_rand` and persisting the result in stable memory. The IC's `raw_rand` output is derived from the threshold random tape and is not manipulable by any single node or observer below the subnet fault threshold.

---

### Proof of Concept

1. Monitor the IC certified state to record the consensus timestamp `T` (nanoseconds) at which the first `add_config` update call is executed on the rate-limit canister.
2. Locally construct the seed:
   ```rust
   let mut seed = [42u8; 32];
   seed[..8].copy_from_slice(&T.to_le_bytes());
   ```
3. Initialize `ChaCha20Rng::from_seed(seed)` and generate 16 bytes via `rng.fill_bytes(&mut buf)`.
4. Construct `Uuid::from_slice(&buf)` — this matches the `RuleId` assigned to the first new rule in that `add_config` call.
5. Continue advancing the RNG state to predict every subsequent `RuleId` in order, across all future `add_config` calls, without any privileged access to the canister.

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

**File:** rs/boundary_node/rate_limits/canister/random.rs (L31-37)
```rust
#[cfg(all(
    target_arch = "wasm32",
    target_vendor = "unknown",
    target_os = "unknown"
))]
getrandom::register_custom_getrandom!(custom_getrandom_bytes_impl);

```

**File:** rs/boundary_node/rate_limits/canister/add_config.rs (L115-116)
```rust
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
