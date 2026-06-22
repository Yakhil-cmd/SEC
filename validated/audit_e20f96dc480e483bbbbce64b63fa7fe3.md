### Title
Off-by-One in `purge_old` Inflates `total_count`, Causing Valid Cycles-Minting Requests to Be Incorrectly Rejected - (File: `rs/nns/cmc/src/limiter.rs`)

---

### Summary

The `Limiter::purge_old` function in the Cycles Minting Canister (CMC) uses `oldest.window + 1` instead of `oldest.window` when computing the purge threshold. This delays eviction of expired time-windows by one full `resolution` period, causing `total_count` to remain inflated beyond the true `max_age` window. As a result, `check_and_add_cycles` can reject valid minting requests that are actually within the configured limit.

---

### Finding Description

`Limiter` tracks cycles minted in the last `max_age` seconds by bucketing events into `resolution`-sized windows and maintaining a running `total_count`. When a new minting request arrives, `purge_old` is called to evict stale windows before the limit check.

The purge condition is:

```rust
// rs/nns/cmc/src/limiter.rs, line 85
if self.window_to_time(oldest.window + 1) + self.max_age <= now {
```

`window_to_time` is:

```rust
// rs/nns/cmc/src/limiter.rs, line 100-102
fn window_to_time(&self, window: TimeWindow) -> SystemTime {
    UNIX_EPOCH + self.resolution * window
}
```

So `window_to_time(oldest.window + 1)` is the **end** of the oldest window (i.e., `start + resolution`), not its start. The condition therefore purges window `W` only when:

```
end_of_window_W + max_age <= now
```

But the semantically correct condition is:

```
start_of_window_W + max_age <= now
```

This delays eviction by exactly `resolution` seconds. During that extra window, `total_count` still includes the stale cycles, and the limit check in `check_and_add_cycles` uses this inflated value:

```rust
// rs/nns/cmc/src/limiter.rs, lines 40-53
pub fn check_and_add_cycles(...) -> Result<(), String> {
    self.purge_old(now);
    let count = self.get_count();
    if count + cycles_to_mint > limit {
        return Err(...);
    }
    ...
}
```

**Concrete example** (using CMC's `resolution = 60s`, `max_age = 3600s`, `limit = 150e15 cycles`):

| Time | Event |
|------|-------|
| t=0 | User mints 100e15 cycles → window 0 gets 100e15 |
| t=3600 | Correct behavior: window 0 is `max_age` old → purge it, `total_count = 0`, new request for 100e15 accepted |
| t=3600 | **Actual behavior**: purge condition checks `window_to_time(1) + 3600 = 60 + 3600 = 3660 > 3600` → NOT purged, `total_count = 100e15`, new request for 100e15 rejected (`100e15 + 100e15 > 150e15`) |
| t=3660 | Window 0 finally purged, request now accepted |

---

### Impact Explanation

Valid cycles-minting requests (`notify_top_up`, `notify_create_canister`, `notify_mint_cycles`) are incorrectly rejected for up to `resolution` seconds after the true `max_age` window has elapsed. The maximum overcounting equals the cycles minted in a single `resolution`-sized bucket. With the production CMC configuration (`resolution = 60s`), users can be blocked for up to 60 extra seconds per cycle of the rate limit. This is a **cycles/resource accounting bug** that causes denial of valid service.

---

### Likelihood Explanation

This is triggered by any unprivileged ingress caller who:
1. Mints cycles close to the limit during a window boundary, then
2. Attempts another minting request exactly when `max_age` has elapsed.

No special privileges are required. The CMC is a production NNS canister reachable by any principal. The condition is deterministic and reproducible.

---

### Recommendation

Replace `oldest.window + 1` with `oldest.window` in the purge condition so that window `W` is evicted as soon as its **start** is older than `max_age`:

```rust
// rs/nns/cmc/src/limiter.rs, line 85
// Before (incorrect):
if self.window_to_time(oldest.window + 1) + self.max_age <= now {

// After (correct):
if self.window_to_time(oldest.window) + self.max_age <= now {
```

---

### Proof of Concept

```rust
// Reproduces the off-by-one: a request that should succeed at exactly t = max_age is rejected.
let resolution = Duration::from_secs(60);
let max_age    = Duration::from_secs(3600);
let limit      = Cycles::new(150_000_000_000_000); // 150T

let mut limiter = Limiter::new(resolution, max_age);
let t0 = UNIX_EPOCH;

// Mint 100T at t=0 (window 0).
limiter.check_and_add_cycles(t0, Cycles::new(100_000_000_000_000), limit).unwrap();

// At t = max_age (3600s), window 0 should be expired and total_count should be 0.
// A request for 100T should succeed (100T <= 150T limit).
let t1 = t0 + max_age; // exactly 3600s later

// BUG: purge_old checks window_to_time(0+1) + 3600 = 60 + 3600 = 3660 > 3600 → NOT purged.
// total_count is still 100T, so 100T + 100T = 200T > 150T → incorrectly rejected.
let result = limiter.check_and_add_cycles(t1, Cycles::new(100_000_000_000_000), limit);
assert!(result.is_err(), "BUG: valid request incorrectly rejected");

// At t = max_age + resolution (3660s), the window is finally purged and the request succeeds.
let t2 = t0 + max_age + resolution;
let result2 = limiter.check_and_add_cycles(t2, Cycles::new(100_000_000_000_000), limit);
assert!(result2.is_ok(), "Request succeeds 60s later than it should");
```

**Root cause location:** [1](#0-0) 

**Limit check that uses the inflated `total_count`:** [2](#0-1) 

**Entry path — `notify_top_up` → `process_top_up` → `deposit_cycles` → `ensure_balance` → `check_and_add_cycles`:** [3](#0-2) 

**`window_to_time` helper confirming `window + 1` maps to the end of the window, not the start:** [4](#0-3)

### Citations

**File:** rs/nns/cmc/src/limiter.rs (L34-56)
```rust
    pub fn check_and_add_cycles(
        &mut self,
        now: SystemTime,
        cycles_to_mint: Cycles,
        limit: Cycles,
    ) -> Result<(), String> {
        self.purge_old(now);
        let count = self.get_count();

        if count + cycles_to_mint > limit {
            LIMITER_REJECT_COUNT.with(|count| {
                count.set(count.get().saturating_add(1));
            });

            return Err(format!(
                "More than {} cycles have been minted in the last {} seconds, please try again later.",
                limit,
                self.get_max_age().as_secs(),
            ));
        }
        self.add(now, cycles_to_mint);
        Ok(())
    }
```

**File:** rs/nns/cmc/src/limiter.rs (L83-92)
```rust
    fn purge_old(&mut self, now: SystemTime) {
        while let Some(oldest) = self.time_windows.front() {
            if self.window_to_time(oldest.window + 1) + self.max_age <= now {
                self.total_count -= oldest.count;
                self.time_windows.pop_front();
            } else {
                break;
            }
        }
    }
```

**File:** rs/nns/cmc/src/limiter.rs (L100-102)
```rust
    fn window_to_time(&self, window: TimeWindow) -> SystemTime {
        UNIX_EPOCH + self.resolution * window
    }
```

**File:** rs/nns/cmc/src/main.rs (L2306-2325)
```rust
fn ensure_balance(
    cycles: Cycles,
    limiter_to_use: CyclesMintingLimiterSelector,
) -> Result<(), String> {
    let now = now_system_time();

    let current_balance = Cycles::from(ic_cdk::api::canister_balance128());
    let cycles_to_mint = cycles - current_balance;

    with_state_mut(|state| {
        limiter_to_use.check_and_add_cycles(state, now, cycles_to_mint)?;
        state.total_cycles_minted += cycles_to_mint;
        Ok::<_, String>(())
    })?;

    // unused because of check above
    let _minted_cycles = ic0_mint_cycles128(cycles_to_mint);
    assert!(ic_cdk::api::canister_balance128() >= cycles.get());
    Ok(())
}
```
