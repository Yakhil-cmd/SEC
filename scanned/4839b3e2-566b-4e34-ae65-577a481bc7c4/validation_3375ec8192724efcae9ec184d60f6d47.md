### Title
Predictable Time-Seeded PRNG Used as Canister's Sole Randomness Source for Rule UUID Generation - (File: rs/boundary_node/rate_limits/canister/random.rs)

### Summary
The rate-limit canister's custom `getrandom` implementation is seeded exclusively with `ic_cdk::api::time()` (the canister's system timestamp) at initialization. This timestamp is a publicly observable, low-entropy value. All UUIDs generated for rate-limit rules via `generate_random_uuid()` in `add_config.rs` are derived from this predictable PRNG, making them fully predictable to any observer who knows the canister's initialization time.

### Finding Description
In `rs/boundary_node/rate_limits/canister/random.rs`, the thread-local `RNG` is initialized as:

```rust
thread_local! {
  static RNG: RefCell<ChaCha20Rng> = {
    let mut seed = [42; 32];
    seed[..8].copy_from_slice(&time().to_le_bytes());
    RefCell::new(ChaCha20Rng::from_seed(seed))
  };
}
```

The seed is constructed by placing the 8-byte IC timestamp into the first 8 bytes of a 32-byte array, with the remaining 24 bytes fixed at the constant `42`. This means the entire 256-bit seed has only 64 bits of variability (the timestamp), and the remaining 192 bits are static and known.

This `RNG` is registered as the canister-wide `getrandom` backend via `getrandom::register_custom_getrandom!(custom_getrandom_bytes_impl)`. The function `generate_random_uuid()` in `rs/boundary_node/rate_limits/canister/add_config.rs` calls `getrandom::getrandom(&mut buf)` to produce 16 bytes for a UUID, which flows directly through this time-seeded RNG.

The IC system time (`ic_cdk::api::time()`) is:
1. Publicly observable — it is the subnet's consensus time, visible to all participants.
2. Coarse-grained — it advances in discrete steps tied to block production, not a high-resolution secret.
3. Fixed at canister initialization — the seed is set once at `thread_local` initialization and never refreshed.

An attacker who knows (or can estimate) the canister's initialization timestamp can reconstruct the exact RNG state and predict all future UUID outputs for the lifetime of the canister.

### Impact Explanation
The rate-limit canister assigns a `RuleId(Uuid)` to each newly submitted rate-limit rule. These UUIDs are used as stable identifiers for rules and incidents. If an attacker can predict the sequence of UUIDs that will be assigned to future rules, they can:

1. **Pre-compute future `RuleId` values** and craft `disclose_rules` calls that reference not-yet-created rules, potentially racing to disclose rules before they are intended to be disclosed.
2. **Enumerate the full UUID sequence** to probe the canister's internal state, correlating rule IDs to submission timing and inferring confidential incident linkages that are supposed to be hidden from `RestrictedRead` callers.
3. **Undermine the confidentiality model**: the canister's access control (`RestrictedRead` vs `FullAccess`) relies on rule IDs being opaque to unauthorized callers. Predictable UUIDs break this opacity assumption.

The impact is a **canister isolation / confidentiality break** in the boundary node rate-limit system: confidential rule metadata (incident linkage, rule content) that is supposed to be hidden from unauthorized callers can be correlated and partially inferred.

### Likelihood Explanation
The IC system time at canister initialization is observable from the public blockchain state (block timestamps are part of certified state). Any party who can query the IC or observe block production can narrow the initialization timestamp to a small window (typically within a single block interval, ~1–2 seconds). With only 64 bits of effective entropy (and 24 bytes fixed at `42`), brute-forcing or narrowing the seed space is practical. The canister is a long-lived system canister; the seed is never refreshed after initialization.

### Recommendation
Replace the time-based seed with a call to `ic00::raw_rand` (the management canister's cryptographically secure randomness API) during canister initialization or first use. Since `raw_rand` is asynchronous, the recommended pattern is to call it in a post-init timer callback and store the result, refusing to generate UUIDs until the secure seed is available. Alternatively, use the same periodic reseeding pattern already used by the NNS governance canister (`rs/nns/governance/src/timer_tasks/seeding.rs`), which calls `raw_rand` on a timer and reseeds the RNG from the result.

The fixed constant `42` filling bytes `[8..32]` of the seed must also be replaced with random bytes.

### Proof of Concept

An attacker observing the IC can:

1. Record the block timestamp at which the rate-limit canister was last initialized/upgraded (publicly available from certified state or block explorers).
2. Reconstruct the seed:
   ```rust
   let mut seed = [42u8; 32];
   seed[..8].copy_from_slice(&observed_init_time_nanos.to_le_bytes());
   let mut rng = ChaCha20Rng::from_seed(seed);
   ```
3. Simulate `generate_random_uuid()` calls:
   ```rust
   fn predict_uuid(rng: &mut ChaCha20Rng) -> Uuid {
       let mut buf = [0u8; 16];
       rng.fill_bytes(&mut buf);
       Uuid::from_slice(&buf).unwrap()
   }
   ```
4. For each `add_config` call observed on-chain, advance the RNG by one UUID and record the predicted `RuleId`.
5. Use predicted `RuleId` values to call `get_rule_by_id` or `disclose_rules` for rules that have not yet been publicly disclosed, bypassing the confidentiality model.

The root cause is entirely within the IC production canister code at: [1](#0-0) 

which feeds into: [2](#0-1) 

called from: [3](#0-2) 

The `insecure_random_u64` pattern in the SNS governance canister explicitly acknowledges this class of weakness: [4](#0-3) 

The NNS governance canister demonstrates the correct mitigation — periodic reseeding from `raw_rand`: [5](#0-4)

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

**File:** rs/boundary_node/rate_limits/canister/add_config.rs (L115-115)
```rust
                let rule_id = RuleId(generate_random_uuid()?);
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

**File:** rs/sns/governance/canister/canister.rs (L102-117)
```rust
            // Seed the pseudo-random number generator (PRNG) with the current time.
            //
            // All replicas are guaranteed to see the same result of now() and the resulting
            // number isn't easily predictable from the outside.
            //
            // Why we don't use raw_rand from the ic00 api instead: this is an asynchronous
            // call so can't really be used to generate random numbers for most cases.
            // It could be used to seed the PRNG, but that wouldn't add any security regarding
            // unpredictability since the pseudo-random numbers could still be predicted after
            // inception.
            rng: {
                let now_nanos = now_nanoseconds() as u128;
                let mut seed = [0_u8; 32];
                seed[..16].copy_from_slice(&now_nanos.to_be_bytes());
                seed[16..32].copy_from_slice(&now_nanos.to_be_bytes());
                ChaCha20Rng::from_seed(seed)
```

**File:** rs/nns/governance/src/timer_tasks/seeding.rs (L31-41)
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
```
