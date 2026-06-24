The code confirms the claim exactly. Let me verify the `create_ratelimiter` initial state and the moka cache eviction behavior.

The code confirms the vulnerability exactly as described. All three code paths are verified:

- `create_ratelimiter` sets `initial_available(burst as u64)` [1](#0-0) , so every newly-created shard starts with a full burst budget.
- The cache is built with both `time_to_idle(tti)` and `max_capacity(max_shards)` [2](#0-1) , enabling both eviction paths.
- `acquire_token` only inserts into `decisions` after `shards.acquire(ip)` returns `false` [3](#0-2) ; if the shard is evicted before that call, `acquire()` returns `true` and no ban is recorded.

---

Audit Report

## Title
Shard Cache Eviction Resets Rate-Limiter State, Permanently Bypassing Bouncer Ban — (`rs/boundary_node/ic_boundary/src/rate_limiting/sharded.rs`, `rs/boundary_node/ic_boundary/src/bouncer/mod.rs`)

## Summary
`ShardedRatelimiter` stores per-IP token buckets in a bounded moka cache with both a capacity limit (`max_shards`) and a time-to-idle (`shard_tti`). When an entry is evicted — either by capacity pressure or TTI expiry — the next call for that IP creates a fresh `Ratelimiter` with `initial_available = burst`, causing `acquire()` to return `true`. Because `Bouncer::acquire_token` only inserts an IP into the `decisions` ban-list when `acquire()` returns `false`, an IP whose shard is evicted before that return value is observed is never banned, permanently defeating the firewall protection.

## Finding Description
**Root cause — `create_ratelimiter`** (`sharded.rs` line 9): every newly-constructed `Ratelimiter` starts with `initial_available(burst as u64)`, so a recreated shard is indistinguishable from a brand-new one.

**Eviction trigger — `ShardedRatelimiter::new`** (`sharded.rs` lines 29–32): the moka cache is built with `.time_to_idle(tti).max_capacity(max_shards)`. Two independent eviction paths exist:
1. **TTI path**: attacker exhausts `burst` tokens from IP A, then sends no requests for `shard_tti` seconds. The idle entry expires. The next request recreates the shard with full burst; `acquire()` returns `true`. Repeatable indefinitely from a single IP.
2. **Capacity path**: attacker fills the cache with `max_shards` distinct IPs, evicting IP A's shard before it returns `false`.

**Ban-recording gap — `acquire_token`** (`bouncer/mod.rs` lines 118–141): the `decisions` DashMap is populated only on the `false` branch of `shards.acquire(ip)`. If the shard is evicted before that branch is reached, the IP is never inserted into `decisions`, and the firewall is never updated for that IP. The existing `decisions.contains_key(&ip)` pre-check (line 120) cannot help because the IP was never inserted.

## Impact Explanation
An attacker can sustain `burst` requests per `shard_tti` seconds from a single IP without ever being added to the nftables blocklist. The Bouncer's core security invariant — "an IP that exceeds its rate limit is firewalled" — is permanently broken for any IP that times its requests around shard eviction. This constitutes a bypass of the boundary node's DoS protection mechanism, enabling sustained overload of boundary nodes and degraded availability for legitimate users. This matches the allowed High impact: **"Application/platform-level DoS… or subnet availability impact not based on raw volumetric DDoS"** and **"Significant boundary/API… infrastructure security impact with concrete user or protocol harm."**

## Likelihood Explanation
The TTI path requires only one IP and knowledge of `shard_tti`, which is a CLI flag (`--bouncer-bucket-ttl`), not a secret. No authentication is required to reach `Bouncer::middleware`. The attacker needs only to send `burst` requests, wait `shard_tti` seconds, and repeat — a trivially automatable loop. The capacity path additionally requires `max_shards` distinct source IPs, feasible with IPv6 or a small botnet. Both paths are repeatable indefinitely with no per-cycle cost increase.

## Recommendation
The ban decision must be persisted independently of the shard cache lifetime:

1. **Eagerly record exhaustion**: when `shards.acquire(ip)` returns `false`, insert into `decisions` immediately (already done) — but also consider tracking "tokens exhausted" state at the moment the last token is consumed rather than at the moment the next request arrives.
2. **Check `decisions` on shard recreation**: in the `get_with` closure in `ShardedRatelimiter::acquire`, or in `acquire_token` before calling `shards.acquire`, verify the IP is not already in a "exhausted" state stored separately from the shard cache.
3. **Decouple ban state from the evictable cache**: maintain a separate, unbounded (or very large) structure that records IPs that have ever exhausted their budget, keyed independently of the moka cache. The moka cache can still be used for token counting; the ban list must not depend on the moka entry surviving.
4. **Alternatively**: use an unbounded moka cache (no `max_capacity`) and rely solely on TTI for cleanup, eliminating the capacity-pressure path — but the TTI path still requires fix (1) or (2).

## Proof of Concept
```rust
// max_shards = 2, burst = 3, shard_tti = 10s
let bouncer = Bouncer::new(10, 3, ban_time, /*max_shards=*/2, Duration::from_secs(10), fw, &reg).unwrap();
let ip_a: IpAddr = "1.0.0.1".parse().unwrap();

// TTI path: exhaust burst for IP A
assert!(bouncer.acquire_token(ip_a)); // token 1
assert!(bouncer.acquire_token(ip_a)); // token 2
assert!(bouncer.acquire_token(ip_a)); // token 3 — shard at 0 tokens

// Wait shard_tti seconds (no requests) — moka evicts IP A's idle shard
std::thread::sleep(Duration::from_secs(11));

// IP A gets a fresh shard with initial_available=burst; acquire() returns true; no ban recorded
assert!(bouncer.acquire_token(ip_a)); // BUG: should be false / banned
assert!(!bouncer.decisions.contains_key(&ip_a)); // IP A is NOT in ban list

// Capacity path variant: fill cache with 2 other IPs to evict IP A's shard
bouncer.acquire_token("2.0.0.1".parse().unwrap());
bouncer.acquire_token("2.0.0.2".parse().unwrap());
// moka evicts IP A's shard (LRU/TinyLFU, capacity=2)
assert!(bouncer.acquire_token(ip_a)); // BUG: fresh shard, no ban
```

### Citations

**File:** rs/boundary_node/ic_boundary/src/rate_limiting/sharded.rs (L9-9)
```rust
        .initial_available(burst as u64)
```

**File:** rs/boundary_node/ic_boundary/src/rate_limiting/sharded.rs (L29-32)
```rust
        let shards = Cache::builder()
            .time_to_idle(tti)
            .max_capacity(max_shards)
            .build();
```

**File:** rs/boundary_node/ic_boundary/src/bouncer/mod.rs (L124-137)
```rust
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
```
