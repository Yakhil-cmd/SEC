### Title
Uninitialized RNG in `randomly_pick_swap_start` Silently Returns Fixed Swap Start Time Instead of Reverting — (`rs/nns/governance/src/governance.rs`)

---

### Summary

`Governance::randomly_pick_swap_start` can be called during the window between canister initialization/upgrade and the completion of the first async `SeedingTask` seeding round. During this window, `CanisterRandomnessGenerator` holds `rng: None`, causing `random_u64()` to return `Err(RngError::RngNotInitialized)`. Rather than propagating this error or reverting, `randomly_pick_swap_start` silently falls back to the hardcoded constant `10_000`, producing a deterministic, predictable SNS swap start time of 2:45:00 UTC for every SNS created during that window.

---

### Finding Description

`CanisterRandomnessGenerator` is constructed with `rng: None`:

```rust
// rs/nns/governance/src/canister_state.rs:130-138
pub struct CanisterRandomnessGenerator {
    rng: Option<ChaCha20Rng>,
}
impl CanisterRandomnessGenerator {
    pub fn new() -> Self {
        CanisterRandomnessGenerator { rng: None }
    }
}
```

The RNG is seeded asynchronously by `SeedingTask`, which calls `raw_rand` on the management canister (`IC_00`) and only seeds the RNG after the async response returns:

```rust
// rs/nns/governance/src/timer_tasks/seeding.rs:31-40
let result: Result<Vec<u8>, (Option<i32>, String)> = env
    .call_canister_method(IC_00, "raw_rand", Encode!().unwrap())
    .await;
// ... seeds only on Ok(bytes)
```

`SeedingTask::initial_delay()` returns `Duration::from_secs(0)`, but on the IC, timer callbacks execute after the current message completes and require at least one async round trip to `raw_rand`. This means there is always a window of at least one IC round after `canister_init` or `canister_post_upgrade` during which `rng` is `None`.

`randomly_pick_swap_start` silently swallows the `RngNotInitialized` error:

```rust
// rs/nns/governance/src/governance.rs:7885-7888
pub fn randomly_pick_swap_start(&mut self) -> GlobalTimeOfDay {
    // It's not critical that we have perfect randomness here, so we can default to a fixed value
    // for the edge case where the RNG is not initialized (which should never happen in practice).
    let time_of_day_seconds = self.randomness.random_u64().unwrap_or(10_000) % ONE_DAY_SECONDS;
```

`10_000 % 86400 = 10000`, rounded down to the nearest 15-minute boundary: `10000 - (10000 % 900) = 9900` seconds after midnight = **2:45:00 UTC**. Every SNS swap whose `CreateServiceNervousSystem` proposal is executed during the uninitialized window will have its start time fixed at this value.

This function is called unconditionally in `do_create_service_nervous_system`:

```rust
// rs/nns/governance/src/governance.rs:4434
let random_swap_start_time = self.randomly_pick_swap_start();
```

The result is passed directly into `make_sns_init_payload` and used as `swap_start_timestamp_seconds` when no explicit `start_time` is provided in the proposal.

---

### Impact Explanation

Any `CreateServiceNervousSystem` proposal executed during the post-init/post-upgrade window before the first `SeedingTask` completes will produce an SNS swap with a deterministic, publicly known start time (2:45:00 UTC). This:

1. **Eliminates the randomness guarantee** that the swap start time is supposed to provide — the purpose of `randomly_pick_swap_start` is to prevent predictability and front-running of SNS token swaps.
2. **Allows front-running**: Participants who know the fixed start time can position themselves to participate at the exact opening moment, gaining an unfair advantage over other participants.
3. **Affects governance integrity**: The NNS governance canister is the root of trust for SNS deployments; a predictable swap start time undermines the fairness guarantees of the SNS launch mechanism.

The existing test infrastructure explicitly acknowledges the uninitialized RNG window as a real operational concern:

```
// rs/rosetta-api/icp/tests/system_tests/common/system_test_environment.rs:453-456
// Give the governance canister some time to initialize so that we do not hit the
// following error:
// Could not claim neuron: Unavailable: Neuron ID generation is not available
// currently. Likely due to uninitialized RNG.
```

---

### Likelihood Explanation

The window exists after **every governance canister upgrade** — a routine operational event. Any `CreateServiceNervousSystem` proposal that was already adopted and queued for execution before the upgrade, or that is submitted and adopted in the first round after the upgrade, will be affected. The IC's deterministic execution model means the window is reproducible and predictable by an observer watching the chain. The `SeedingTask` retry interval on failure is 30 seconds, meaning if `raw_rand` fails, the window extends significantly.

---

### Recommendation

`randomly_pick_swap_start` should propagate the `RngNotInitialized` error rather than silently falling back to a hardcoded constant. The function signature should return `Result<GlobalTimeOfDay, GovernanceError>`, and `do_create_service_nervous_system` should return an error if the RNG is not yet seeded, allowing the proposal execution to be retried once the RNG is available:

```rust
pub fn randomly_pick_swap_start(&mut self) -> Result<GlobalTimeOfDay, GovernanceError> {
    let time_of_day_seconds = self.randomness.random_u64()
        .map_err(GovernanceError::from)?  // propagate RngNotInitialized
        % ONE_DAY_SECONDS;
    let remainder_seconds = time_of_day_seconds % (15 * 60);
    Ok(GlobalTimeOfDay {
        seconds_after_utc_midnight: Some(time_of_day_seconds - remainder_seconds),
    })
}
```

Alternatively, the `SeedingTask` should be completed synchronously during `canister_init` and `canister_post_upgrade` before any proposal execution is permitted, eliminating the uninitialized window entirely.

---

### Proof of Concept

1. Deploy or upgrade the NNS governance canister.
2. In the **same round** (before `SeedingTask` completes its first `raw_rand` call), execute a `CreateServiceNervousSystem` proposal that omits `swap_parameters.start_time`.
3. Observe that `randomly_pick_swap_start` is called with `rng = None`.
4. `random_u64()` returns `Err(RngNotInitialized)`.
5. `unwrap_or(10_000)` silently substitutes `10_000`.
6. `10_000 % 86400 = 10000`; rounded to nearest 15 min = `9900` seconds after midnight.
7. The SNS swap `swap_start_timestamp_seconds` is set to a deterministic, publicly predictable value (2:45:00 UTC) rather than a random one.
8. Any participant monitoring the chain can observe this fixed start time and front-run the swap opening.

**Relevant files and lines:**
- `CanisterRandomnessGenerator` initialized with `rng: None`: [1](#0-0) 
- `random_u64()` returns `Err` when uninitialized: [2](#0-1) 
- `randomly_pick_swap_start` silently falls back to `10_000`: [3](#0-2) 
- Called unconditionally during SNS creation: [4](#0-3) 
- `SeedingTask` seeds RNG only after async `raw_rand` returns: [5](#0-4) 
- Test acknowledges uninitialized RNG window as real operational concern: [6](#0-5)

### Citations

**File:** rs/nns/governance/src/canister_state.rs (L130-138)
```rust
#[derive(Default)]
pub struct CanisterRandomnessGenerator {
    rng: Option<ChaCha20Rng>,
}

impl CanisterRandomnessGenerator {
    pub fn new() -> Self {
        CanisterRandomnessGenerator { rng: None }
    }
```

**File:** rs/nns/governance/src/canister_state.rs (L141-147)
```rust
impl RandomnessGenerator for CanisterRandomnessGenerator {
    fn random_u64(&mut self) -> Result<u64, RngError> {
        match self.rng.as_mut() {
            Some(rand) => Ok(rand.next_u64()),
            None => Err(RngError::RngNotInitialized),
        }
    }
```

**File:** rs/nns/governance/src/governance.rs (L4434-4434)
```rust
        let random_swap_start_time = self.randomly_pick_swap_start();
```

**File:** rs/nns/governance/src/governance.rs (L7885-7897)
```rust
    pub fn randomly_pick_swap_start(&mut self) -> GlobalTimeOfDay {
        // It's not critical that we have perfect randomness here, so we can default to a fixed value
        // for the edge case where the RNG is not initialized (which should never happen in practice).
        let time_of_day_seconds = self.randomness.random_u64().unwrap_or(10_000) % ONE_DAY_SECONDS;

        // Round down to nearest multiple of 15 min.
        let remainder_seconds = time_of_day_seconds % (15 * 60);
        let seconds_after_utc_midnight = Some(time_of_day_seconds - remainder_seconds);

        GlobalTimeOfDay {
            seconds_after_utc_midnight,
        }
    }
```

**File:** rs/nns/governance/src/timer_tasks/seeding.rs (L31-50)
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
            Err((code, msg)) => {
                println!(
                    "{}Error seeding RNG. Error Code: {:?}. Error Message: {}",
                    LOG_PREFIX, code, msg
                );
                RETRY_SEEDING_INTERVAL
            }
        };
```

**File:** rs/rosetta-api/icp/tests/system_tests/common/system_test_environment.rs (L453-459)
```rust
            // Give the governance canister some time to initialize so that we do not hit the
            // following error:
            // Could not claim neuron: Unavailable: Neuron ID generation is not available
            // currently. Likely due to uninitialized RNG.
            pocket_ic
                .advance_time(std::time::Duration::from_secs(60))
                .await;
```
