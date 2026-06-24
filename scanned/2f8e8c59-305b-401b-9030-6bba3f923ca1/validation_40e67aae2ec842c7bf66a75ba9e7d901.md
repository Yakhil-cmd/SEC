### Title
Shard Cache Eviction Resets Rate-Limiter State, Permanently Bypassing Bouncer Ban — (`rs/boundary_node/ic_boundary/src/rate_limiting/sharded.rs`, `src/bouncer/mod.rs`)

---

### Summary

The `Bouncer` bans an IP only when `ShardedRatelimiter::acquire()` returns `false`. Because the underlying shard lives in a bounded `moka::sync::Cache`, it can be evicted before that `false` is ever returned. Once evicted, the next call for that IP creates a fresh `Ratelimiter` with a full burst budget, `acquire()` returns `true`, and no ban is recorded. An attacker who controls enough distinct source IPs can keep evicting their own shard indefinitely, sustaining a continuous burst stream without ever being firewalled.

---

### Finding Description

**`ShardedRatelimiter::acquire`** — `sharded.rs` lines 42-48:

```rust
pub fn acquire(&self, key: K) -> bool {
    let shard = self.shards.get_with(key, || Shard {
        limiter: Arc::new(create_ratelimiter(self.limit, self.burst, self.dur)),
    });
    shard.limiter.try_wait().is_ok()
}
``` [1](#0-0) 

The cache is built with `max_capacity(max_shards)` and `time_to_idle(tti)`. [2](#0-1) 

When the cache is full and a new key is inserted via `get_with`, moka evicts existing entries. If IP A's shard is evicted, the closure fires and a brand-new `Ratelimiter` with `initial_available = burst` is returned — `try_wait()` succeeds and `acquire()` returns `true`.

**`Bouncer::acquire_token`** — `bouncer/mod.rs` lines 118-141:

```rust
fn acquire_token(&self, ip: IpAddr) -> bool {
    if self.decisions.contains_key(&ip) { return false; }
    if self.shards.acquire(ip) { return true; }          // ← ban only if this returns false
    self.decisions.insert(ip, Decision { ... });
    false
}
``` [3](#0-2) 

The `decisions` DashMap (the ban list) is entirely separate from the `shards` cache. Evicting a shard does **not** add the IP to `decisions`. The ban is only recorded when `acquire()` returns `false`, which requires the shard to be present **and** exhausted. If the shard is gone, the IP is never banned.

**Two independent eviction paths exist:**

1. **Capacity pressure** — attacker sends requests from `max_shards` distinct IPs, filling the cache and evicting IP A's shard.
2. **TTI expiry** — attacker sends `burst` requests from IP A, waits `shard_tti` seconds (no requests needed from other IPs), shard expires, then sends another full burst. This path requires zero additional IPs. [4](#0-3) 

---

### Impact Explanation

An attacker can sustain a request rate of `burst` requests per `shard_tti` seconds from a single IP (TTI path) or `burst` requests per eviction cycle (capacity path) without ever being added to the firewall blocklist. The Bouncer's core invariant — "an IP that exceeds its rate limit is banned" — is permanently broken for any IP that times its requests to exploit shard eviction.

---

### Likelihood Explanation

- **TTI path**: requires only one IP and knowledge of `shard_tti` (a CLI flag, not a secret). Trivially exploitable.
- **Capacity path**: requires controlling `max_shards` distinct IPs, feasible with a modest botnet or IPv6 address space.
- The boundary node is a public ingress point; no authentication is required to reach `Bouncer::middleware`. [5](#0-4) 

---

### Recommendation

The ban decision must be persisted independently of the shard cache. Two complementary fixes:

1. **Persist exhausted state in `decisions` eagerly**: when a shard's token count reaches zero (or on the first `acquire() == false`), immediately insert the IP into `decisions` before the shard can be evicted.
2. **On shard recreation, check `decisions` first**: `acquire_token` already does `decisions.contains_key` before calling `shards.acquire`, so a banned IP is still blocked even after shard eviction. The gap is that the IP is only inserted into `decisions` *after* `acquire()` returns `false` — but if the shard is evicted before that call, `acquire()` returns `true` and the insertion never happens. The fix is to track "tokens exhausted" state in `decisions` at the moment the last token is consumed, not at the moment the next request arrives.
3. **Alternatively**: replace the bounded moka cache with an unbounded one (or one large enough that eviction under realistic load is impossible) and rely solely on TTI for cleanup. This eliminates the capacity-pressure path but not the TTI path, so fix (1) or (2) is still needed.

---

### Proof of Concept

```rust
// max_shards = 2, burst = 3
let bouncer = Bouncer::new(10, 3, ban_time, /*max_shards=*/2, shard_tti, fw, &reg).unwrap();
let ip_a: IpAddr = "1.0.0.1".parse().unwrap();

// Exhaust IP A's burst (3 tokens)
assert!(bouncer.acquire_token(ip_a)); // token 1
assert!(bouncer.acquire_token(ip_a)); // token 2
assert!(bouncer.acquire_token(ip_a)); // token 3 — shard now at 0 tokens

// Evict IP A's shard by filling cache with 2 other IPs
bouncer.acquire_token("2.0.0.1".parse().unwrap());
bouncer.acquire_token("2.0.0.2".parse().unwrap());
// moka evicts IP A's shard (LRU/TinyLFU, capacity=2)

// IP A gets a fresh shard — acquire returns true, no ban recorded
assert!(bouncer.acquire_token(ip_a)); // BUG: should be false / banned
assert!(!bouncer.decisions.contains_key(&ip_a)); // IP A is NOT in ban list
``` [3](#0-2) [1](#0-0)

### Citations

**File:** rs/boundary_node/ic_boundary/src/rate_limiting/sharded.rs (L28-40)
```rust
    pub fn new(limit: u32, burst: u32, dur: Duration, tti: Duration, max_shards: u64) -> Self {
        let shards = Cache::builder()
            .time_to_idle(tti)
            .max_capacity(max_shards)
            .build();

        Self {
            shards,
            limit,
            burst,
            dur,
        }
    }
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

**File:** rs/boundary_node/ic_boundary/src/bouncer/mod.rs (L278-300)
```rust
pub async fn middleware(
    State(bouncer): State<Arc<Bouncer>>,
    request: Request<Body>,
    next: Next,
) -> Result<impl IntoResponse, ErrorCause> {
    // Attempt to extract client's IP from the request
    let ip = request
        .extensions()
        .get::<Arc<ConnInfo>>()
        .map(|x| x.remote_addr.ip());

    if let Some(v) = ip {
        if !bouncer.acquire_token(v) {
            return Err(ErrorCause::RateLimited(RateLimitCause::Bouncer));
        }
    } else {
        // This should not really happen ever, unless somebody enables bouncer when running with Unix socket.
        // Maybe we should check that and forbid or add IP extraction using X-Real-IP & friends headers.
        return Err(ErrorCause::Other("Unable to extract client's IP".into()));
    }

    Ok(next.run(request).await)
}
```
