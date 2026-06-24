### Title
Severely Limited PRNG Seed Entropy in Rate Limits Canister `getrandom` Implementation — (File: rs/boundary_node/rate_limits/canister/random.rs)

---

### Summary

The boundary node rate limits canister registers a custom `getrandom` implementation backed by a `ChaCha20Rng` seeded with only 8 bytes of time entropy; the remaining 24 bytes of the 32-byte seed are the constant `0x2a` (42). This limits the effective RNG state space to at most 2^64 distinct streams — directly analogous to the Canto Identity `iteratePRNG % 2038074743` bug, where a small modulus collapsed the PRNG output space far below the theoretical maximum.

---

### Finding Description

In `rs/boundary_node/rate_limits/canister/random.rs`, the thread-local `ChaCha20Rng` is initialized as follows:

```rust
thread_local! {
  static RNG: RefCell<ChaCha20Rng> = {
    let mut seed = [42; 32];
    seed[..8].copy_from_slice(&time().to_le_bytes());
    RefCell::new(ChaCha20Rng::from_seed(seed))
  };
}
``` [1](#0-0) 

Only the first 8 bytes of the 32-byte seed are populated from `ic_cdk::api::time()` (the IC nanosecond timestamp). The remaining 24 bytes are always `42`. A `ChaCha20Rng` seeded this way can occupy at most 2^64 distinct initial states — one per possible 64-bit nanosecond timestamp — rather than the 2^256 states a properly seeded instance would have.

This weakly-seeded RNG is then registered as the canister-wide `getrandom` provider:

```rust
getrandom::register_custom_getrandom!(custom_getrandom_bytes_impl);
``` [2](#0-1) 

Every call to `getrandom` anywhere in the canister — including from third-party dependencies performing cryptographic operations — draws from this single, weakly-seeded stream.

The structural parallel to the Canto Identity bug is exact:

| | Canto Identity | Rate Limits Canister |
|---|---|---|
| Theoretical space | 2^256 (seed) | 2^256 (ChaCha20 seed) |
| Actual space | ~2^31 (`% 2038074743`) | ~2^64 (8-byte time seed) |
| Cause | Small modulus on PRNG output | Constant padding of 24/32 seed bytes |

---

### Impact Explanation

Any security-sensitive operation in the rate limits canister that relies on `getrandom` — nonce generation, UUID generation, cryptographic key derivation, token generation — will have at most 64 bits of effective entropy. An attacker who can observe the canister's initialization time (which is recorded on-chain and queryable via the IC management canister) can enumerate all candidate seeds within a nanosecond-resolution window and reproduce the full RNG output stream. This enables prediction or forgery of any value the canister generates via `getrandom`, potentially allowing bypass of rate-limit enforcement mechanisms that depend on unpredictable tokens or nonces.

---

### Likelihood Explanation

The IC canister creation timestamp is deterministic, consensus-agreed, and publicly observable. An attacker needs no privileged access: they query the canister's creation time, enumerate nanosecond timestamps in a bounded window (IC block times are ~1–2 seconds, so the search space is at most ~2×10^9 candidates per second of uncertainty), construct the corresponding seeds, and compare predicted RNG outputs against observed canister behavior to identify the correct seed. No threshold corruption, admin key, or social engineering is required.

---

### Recommendation

Replace the time-based seed with a full 32 bytes of entropy. The correct approach on the IC is to call `ic_cdk::api::management_canister::main::raw_rand()` during `canister_init` (accepting the asynchronous cost at initialization time only) and store the resulting 32-byte seed for use in the thread-local RNG. Alternatively, combine the time with a canister-specific secret or use the IC's VRF-based randomness beacon.

---

### Proof of Concept

1. Query the rate limits canister's creation timestamp `T` from the IC management canister (`canister_status`).
2. For each candidate nanosecond timestamp `t` in `[T - ε, T + ε]`:
   - Construct `seed = [42u8; 32]` with `seed[..8] = t.to_le_bytes()`
   - Initialize `ChaCha20Rng::from_seed(seed)`
   - Generate the first N bytes of output
3. Compare predicted outputs against any observable output from the canister's `getrandom`-backed operations (e.g., generated nonces or tokens visible in canister responses).
4. Once the matching seed is identified, the attacker can predict all future `getrandom` outputs from the canister for the lifetime of the thread-local RNG — mirroring exactly how the Canto Identity attacker, knowing the 7-value `numAbove` seed, could enumerate all `7 × 2038074743` reachable Zalgo distortions. [3](#0-2)

### Citations

**File:** rs/boundary_node/rate_limits/canister/random.rs (L1-38)
```rust
use ic_cdk::api::time;
use rand_chacha::ChaCha20Rng;
#[cfg(target_family = "wasm")]
use rand_chacha::rand_core::RngCore;
use rand_chacha::rand_core::SeedableRng;
use std::cell::RefCell;

thread_local! {
  static RNG: RefCell<ChaCha20Rng> = {
    let mut seed = [42; 32];
    seed[..8].copy_from_slice(&time().to_le_bytes());
    RefCell::new(ChaCha20Rng::from_seed(seed))
  };
}

#[cfg(all(
    target_arch = "wasm32",
    target_vendor = "unknown",
    target_os = "unknown"
))]
/// A getrandom implementation that works in the IC.
pub fn custom_getrandom_bytes_impl(dest: &mut [u8]) -> Result<(), getrandom::Error> {
    RNG.with(|rng| {
        let mut rng = rng.borrow_mut();
        rng.fill_bytes(dest);
    });

    Ok(())
}

#[cfg(all(
    target_arch = "wasm32",
    target_vendor = "unknown",
    target_os = "unknown"
))]
getrandom::register_custom_getrandom!(custom_getrandom_bytes_impl);


```
