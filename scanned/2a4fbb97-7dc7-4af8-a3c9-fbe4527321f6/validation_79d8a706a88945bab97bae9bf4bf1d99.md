### Title
Predictable Time-Based RNG Seed in SNS Governance Canister Enables Foreseeable Random Outputs - (File: rs/sns/governance/canister/canister.rs)

### Summary
The SNS Governance canister's `CanisterEnv` initializes its ChaCha20 RNG by seeding it exclusively with `now_nanoseconds()` (the IC consensus time at canister initialization), repeated twice to fill 32 bytes. Because the IC consensus time is deterministic, publicly observable, and recorded in canister history, any external observer can reconstruct the exact RNG seed and predict every future output of `insecure_random_u64()`.

### Finding Description
In `rs/sns/governance/canister/canister.rs`, `CanisterEnv::new()` constructs the RNG as follows:

```rust
rng: {
    let now_nanos = now_nanoseconds() as u128;
    let mut seed = [0_u8; 32];
    seed[..16].copy_from_slice(&now_nanos.to_be_bytes());
    seed[16..32].copy_from_slice(&now_nanos.to_be_bytes());
    ChaCha20Rng::from_seed(seed)
},
``` [1](#0-0) 

The seed is `now_nanoseconds()` duplicated — only 16 bytes of entropy, derived entirely from the IC consensus time at canister initialization. The inline comment claims this value "isn't easily predictable from the outside," but this is incorrect: the IC consensus time is deterministic, agreed upon by all replicas, and the canister's initialization timestamp is permanently visible in the canister's on-chain history.

The `RandomnessGenerator` trait used by NNS Governance correctly seeds from `raw_rand` (a threshold-BLS-backed entropy source) via `SeedingTask`, reseeding every hour:

```rust
let result: Result<Vec<u8>, (Option<i32>, String)> = env
    .call_canister_method(IC_00, "raw_rand", Encode!().unwrap())
    .await;
``` [2](#0-1) 

SNS Governance has no equivalent reseeding mechanism. Its `CanisterEnv` is initialized once at canister start and the RNG state advances deterministically from that fixed seed for the lifetime of the canister. [3](#0-2) 

### Impact Explanation
An unprivileged observer who knows the SNS governance canister's initialization timestamp (publicly readable from the IC) can:

1. Reconstruct the exact 32-byte seed (`now_nanos` repeated twice).
2. Instantiate an identical `ChaCha20Rng` locally.
3. Replay the exact sequence of `insecure_random_u64()` calls that have occurred (inferable from on-chain state: number of neurons created, proposals processed, etc.).
4. Predict every future output of `insecure_random_u64()`.

This affects any SNS governance operation that relies on `insecure_random_u64` for randomness-dependent outcomes — including neuron ID generation and any other governance randomness. An attacker can pre-compute which neuron IDs will be assigned to future neurons, enabling front-running of neuron creation or targeted manipulation of governance state that depends on those IDs.

### Likelihood Explanation
The attack requires only:
- Reading the canister's initialization time from the public IC state (zero privilege required).
- Counting how many `insecure_random_u64` calls have been made (inferable from public canister state).
- Running a local ChaCha20 simulation (trivial computation).

No privileged access, no threshold corruption, no social engineering. Any unprivileged ingress sender or canister caller can execute this.

### Recommendation
Replace the time-based seed with a call to `ic0::raw_rand` (or the management canister's `raw_rand` method) at canister initialization, and periodically reseed — mirroring the pattern already used by NNS Governance's `SeedingTask`: [4](#0-3) 

Until `raw_rand` is available (it requires an async call), the canister should at minimum defer any randomness-dependent operations until after the first `raw_rand` response is received, rather than using a time-derived seed at construction time.

### Proof of Concept

```
1. Query the IC for the SNS governance canister's initialization timestamp T (nanoseconds).
2. Construct seed: seed[0..16] = T.to_be_bytes(); seed[16..32] = T.to_be_bytes()
3. Initialize ChaCha20Rng::from_seed(seed) locally.
4. Count N = number of insecure_random_u64 calls made so far
   (= neurons created + other randomness consumers, readable from public canister state).
5. Advance the local RNG by N steps.
6. The next output of rng.next_u64() exactly matches the next value
   that insecure_random_u64() will return inside the SNS governance canister.
7. An attacker can use this to predict the neuron ID that will be assigned
   to the next neuron created, enabling front-running or targeted manipulation.
``` [5](#0-4)

### Citations

**File:** rs/sns/governance/canister/canister.rs (L99-121)
```rust
impl CanisterEnv {
    fn new() -> Self {
        CanisterEnv {
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
            },
            time_warp: TimeWarp { delta_s: 0 },
        }
    }
```

**File:** rs/sns/governance/canister/canister.rs (L134-137)
```rust
    // Returns a random u64.
    fn insecure_random_u64(&mut self) -> u64 {
        self.rng.next_u64()
    }
```

**File:** rs/nns/governance/src/timer_tasks/seeding.rs (L19-22)
```rust
// Seeding interval seeks to find a balance between the need for rng secrecy, and
// avoiding the overhead of frequent reseeding.
const SEEDING_INTERVAL: Duration = Duration::from_secs(3600);
const RETRY_SEEDING_INTERVAL: Duration = Duration::from_secs(30);
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
