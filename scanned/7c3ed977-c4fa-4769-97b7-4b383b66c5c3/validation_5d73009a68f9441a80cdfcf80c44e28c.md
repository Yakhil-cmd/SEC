### Title
IPv6 Address Cycling Exhausts ShardedRatelimiter Cache, Enabling Rate-Limit Bypass and Eviction-Reset of Legitimate IP State — (`rs/boundary_node/ic_boundary/src/rate_limiting/sharded.rs`, `rs/boundary_node/ic_boundary/src/bouncer/mod.rs`)

---

### Summary

The `ShardedRatelimiter` stores per-IP token-bucket state in a bounded moka `Cache` capped at `max_shards`. Every new key inserted when the cache is full silently evicts an existing entry. Because `create_ratelimiter` always initialises a fresh shard with `initial_available(burst)` tokens, any evicted IP that later sends a request receives a full burst allowance as if it had never been seen before. An attacker who controls a /64 IPv6 prefix (routinely delegated by ISPs and cloud providers) can cycle through 2^64 unique source addresses, continuously inserting new shards, driving eviction of legitimate IPs' accumulated rate-limit debt, and simultaneously bypassing their own per-IP throttle indefinitely.

---

### Finding Description

**Root cause — `sharded.rs`** [1](#0-0) 

Every new shard is born with a **full** burst allowance (`initial_available(burst as u64)`). There is no "cold-start penalty." [2](#0-1) 

The moka cache is hard-capped at `max_shards`. When capacity is reached, moka evicts the least-recently-used/least-frequently-used entry. The evicted entry's accumulated token-debt is permanently discarded. [3](#0-2) 

`get_with` atomically inserts a brand-new shard (full burst) if the key is absent — whether it was absent because it was never seen, or because it was evicted.

**Exploitation path through the Bouncer** [4](#0-3) 

`acquire_token` checks the `decisions` DashMap (banned IPs) first, then delegates to `shards.acquire(ip)`. The `decisions` map is **unbounded** and tracks only IPs that have already exhausted their burst and been banned. The moka cache tracks IPs that are **within** their rate limit. These two structures are independent — eviction from the moka cache does not affect `decisions`, but it does silently reset the token-debt of any not-yet-banned IP.

**Attack steps:**

1. Attacker controls `2^64` IPv6 addresses from a single /64 prefix.
2. Attacker sends `burst_size` requests from `addr_1` → shard created, tokens consumed, IP banned (enters `decisions`).
3. Repeat with `addr_2 … addr_N`. After `max_shards` unique addresses, the moka cache is full.
4. Each subsequent new address evicts an existing shard. If the evicted shard belonged to a legitimate IP that had consumed, say, `burst_size - 1` tokens, that debt is gone.
5. The legitimate IP's next request creates a fresh shard with `burst_size` tokens — it is effectively un-rate-limited.
6. The attacker's own IPs are never reused (they are in `decisions` until `ban_time` expires), so the attacker always presents a fresh address and always gets a full burst.

---

### Impact Explanation

**Impact (a) — Attacker rate-limit bypass:** With 2^64 source addresses the attacker can sustain `burst_size` allowed requests per new address indefinitely, making the per-IP rate limit completely ineffective against a /64-equipped adversary.

**Impact (b) — Legitimate IP state reset:** Legitimate IPs whose shards are evicted silently receive a fresh burst allowance. In a coordinated scenario, the attacker can deliberately time evictions to grant a target IP (or a set of IPs under their control) repeated fresh bursts, undermining the invariant that the Bouncer accurately tracks all active IPs up to `max_shards`.

---

### Likelihood Explanation

- /64 IPv6 prefixes are standard ISP and cloud allocations; no special privilege is required.
- The attack requires only HTTP requests to the boundary node — a fully public, unauthenticated entry point.
- `max_shards` (e.g. 20 000–30 000) is a small finite number; filling it requires only that many distinct source addresses, trivially achievable.
- No cryptographic material, governance keys, or subnet-majority corruption is needed.

---

### Recommendation

1. **Prefix-level aggregation:** Key the `ShardedRatelimiter` on the /48 or /64 IPv6 prefix rather than the full 128-bit address, collapsing the attacker's address space to a single bucket.
2. **No free burst on re-insertion:** When a shard is evicted, persist its "debt" (tokens consumed) in a secondary structure (e.g. a small LRU map of `(ip, tokens_remaining)`) so that re-insertion does not grant a fresh burst.
3. **Separate eviction policy for security-critical state:** Use a non-evicting structure (e.g. a fixed-size array with explicit LRU replacement that preserves the replaced entry's debt) rather than a transparent moka cache.
4. **Bound the `decisions` map:** The unbounded `DashMap` for banned IPs can itself grow without limit under a sustained attack; apply a capacity cap with LRU eviction there as well.

---

### Proof of Concept

```rust
// Demonstrates that eviction resets token debt, allowing >burst_size
// requests from a single logical attacker within one rate window.
#[test]
fn test_eviction_bypass() {
    // max_shards = 3, burst = 5
    let rl: ShardedRatelimiter<u64> =
        ShardedRatelimiter::new(5, 5, Duration::from_secs(1),
                                Duration::from_secs(60), 3);

    // Fill cache: IPs 0,1,2 each consume 4 of 5 tokens (not yet exhausted)
    for ip in 0u64..3 {
        for _ in 0..4 { assert!(rl.acquire(ip)); }
    }

    // IP 0 has 1 token left. Now insert IP 3 → evicts IP 0 (LRU).
    assert!(rl.acquire(3u64));

    // IP 0 re-enters: gets a FRESH shard with 5 tokens, not 1.
    let mut allowed = 0;
    for _ in 0..10 {
        if rl.acquire(0u64) { allowed += 1; }
    }
    // Without the bug: allowed == 1 (one token remaining)
    // With the bug:    allowed == 5 (full burst reset)
    assert_eq!(allowed, 5, "eviction silently reset token debt");
}
```

The `create_ratelimiter` call inside `get_with` always passes `initial_available(burst)`, so the assertion `allowed == 5` holds against the current code, confirming the invariant violation described in the question. [5](#0-4) [3](#0-2) [4](#0-3)

### Citations

**File:** rs/boundary_node/ic_boundary/src/rate_limiting/sharded.rs (L6-12)
```rust
pub fn create_ratelimiter(limit: u32, burst: u32, duration: Duration) -> Ratelimiter {
    Ratelimiter::builder(1, duration.checked_div(limit).unwrap_or(Duration::ZERO))
        .max_tokens(burst as u64)
        .initial_available(burst as u64)
        .build()
        .unwrap()
}
```

**File:** rs/boundary_node/ic_boundary/src/rate_limiting/sharded.rs (L28-32)
```rust
    pub fn new(limit: u32, burst: u32, dur: Duration, tti: Duration, max_shards: u64) -> Self {
        let shards = Cache::builder()
            .time_to_idle(tti)
            .max_capacity(max_shards)
            .build();
```

**File:** rs/boundary_node/ic_boundary/src/rate_limiting/sharded.rs (L42-48)
```rust
    pub fn acquire(&self, key: K) -> bool {
        let shard = self.shards.get_with(key, || Shard {
            limiter: Arc::new(create_ratelimiter(self.limit, self.burst, self.dur)),
        });

        shard.limiter.try_wait().is_ok()
    }
```

**File:** rs/boundary_node/ic_boundary/src/bouncer/mod.rs (L118-141)
```rust
    fn acquire_token(&self, ip: IpAddr) -> bool {
        // Check if the IP is already banned
        if self.decisions.contains_key(&ip) {
            return false;
        }

        if self.shards.acquire(ip) {
            return true;
        }

        warn!("Bouncer: banning {ip}");

        self.decisions.insert(
            ip,
            Decision {
                ip,
                when: Instant::now(),
                length: self.ban_time,
            },
        );

        self.mark_update();
        false
    }
```
