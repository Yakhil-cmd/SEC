### Title
Weak PRNG Seeding with Mostly-Constant Value Produces Predictable UUIDs for Rate-Limit Rules — (File: `rs/boundary_node/rate_limits/canister/random.rs`)

---

### Summary

The production rate-limit canister registers a custom `getrandom` implementation seeded with 24 bytes of the constant `42` and only 8 bytes of the IC batch time. This makes all UUIDs generated for rate-limit rule identifiers (`RuleId`) predictable to any observer who knows the canister's initialization or upgrade timestamp — a value that is publicly observable on-chain.

---

### Finding Description

In `rs/boundary_node/rate_limits/canister/random.rs`, the canister's global PRNG is initialized as:

```rust
thread_local! {
  static RNG: RefCell<ChaCha20Rng> = {
    let mut seed = [42; 32];
    seed[..8].copy_from_slice(&time().to_le_bytes());
    RefCell::new(ChaCha20Rng::from_seed(seed))
  };
}
``` [1](#0-0) 

The 32-byte ChaCha20 seed has 24 bytes hardcoded to `42` and only 8 bytes drawn from `ic_cdk::api::time()` (nanoseconds since Unix epoch). The `ic_cdk::api::time()` value is the **batch time** — it is deterministic across all replicas in a subnet and is publicly observable on-chain.

This RNG is registered as the global `getrandom` implementation for the canister:

```rust
getrandom::register_custom_getrandom!(custom_getrandom_bytes_impl);
``` [2](#0-1) 

In `add_config.rs`, every new rate-limit rule receives a UUID generated via this RNG:

```rust
fn generate_random_uuid() -> Result<Uuid, anyhow::Error> {
    let mut buf = [0_u8; 16];
    getrandom::getrandom(&mut buf)
        ...
    let uuid = Uuid::from_slice(&buf)...;
    Ok(uuid)
}
``` [3](#0-2) 

This function is called for every new rule submitted via `add_config`: [4](#0-3) 

The canister is a deployed production canister (installed via NNS proposal, confirmed by `rs/boundary_node/rate_limits/proposals/install_10-01-2025_134775.md`). The `post_upgrade` hook calls `init`, which re-initializes the RNG on every upgrade: [5](#0-4) 

---

### Impact Explanation

An external observer who knows the canister's initialization or upgrade timestamp (publicly available via on-chain state) can reconstruct the exact 32-byte seed (`[42,42,...,42, <8 bytes of time>, 42,...,42]`) and predict the entire sequence of `RuleId` UUIDs that will be generated for future rate-limit rules. This breaks the assumption that rule identifiers are unguessable. While `RuleId` values are not currently used as secrets (they are returned in `get_config` responses), the effective entropy of the UUID space is reduced from 128 bits to at most 64 bits of publicly observable time — and in practice far less, since IC batch time advances in coarse, predictable increments. Any future use of these identifiers in a security-sensitive context (e.g., as capability tokens or access-control keys) would be immediately compromised. Additionally, because the RNG state is deterministic from a known seed, an attacker can enumerate all future rule IDs before they are created, enabling targeted interference with the canister's append-only audit log.

---

### Likelihood Explanation

The canister's initialization time is recorded on-chain and is trivially observable by any IC user. The seed construction is straightforward to reverse: 24 of 32 bytes are the constant `42`, and the remaining 8 bytes are the public batch time. Any party monitoring the IC can reconstruct the seed immediately after a canister install or upgrade. The rate-limit canister is upgraded periodically (as evidenced by the upgrade proposals in the repository), resetting the RNG each time and making the seed freshly predictable after each upgrade.

---

### Recommendation

Replace the time-seeded, mostly-constant initialization with a call to `ic00::raw_rand` at canister startup to obtain cryptographically unpredictable seed material from the IC's threshold randomness beacon. Since `raw_rand` is asynchronous, it should be invoked in a timer callback immediately after `init`/`post_upgrade` and used to re-seed the RNG before any UUID generation occurs. This is the pattern already used by the NNS governance canister: [6](#0-5) 

---

### Proof of Concept

1. Observe the canister's initialization timestamp `T` (nanoseconds) from on-chain state after install or upgrade.
2. Reconstruct the seed:
   ```
   seed = [42; 32]
   seed[0..8] = T.to_le_bytes()
   ```
3. Initialize `ChaCha20Rng::from_seed(seed)`.
4. For each subsequent `add_config` call that introduces a new rule, call `rng.fill_bytes(&mut buf[0..16])` and construct `Uuid::from_slice(&buf)` — this yields the exact `RuleId` that the canister will assign to that rule, before the transaction is finalized.

The root cause is the hardcoded constant `[42; 32]` base seed with only 8 bytes of public, deterministic time mixed in, directly analogous to zkSync's hardcoded `block.difficulty = 25 * 10^15` — both substitute a constant or low-entropy value where an unpredictable protocol-level random value is required.

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

**File:** rs/boundary_node/rate_limits/canister/random.rs (L31-37)
```rust
#[cfg(all(
    target_arch = "wasm32",
    target_vendor = "unknown",
    target_os = "unknown"
))]
getrandom::register_custom_getrandom!(custom_getrandom_bytes_impl);

```

**File:** rs/boundary_node/rate_limits/canister/add_config.rs (L114-116)
```rust
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

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L99-104)
```rust
// Run every time a canister is upgraded
#[post_upgrade]
fn post_upgrade(init_arg: InitArg) {
    // Run the same initialization logic
    init(init_arg);
}
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
