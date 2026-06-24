Audit Report

## Title
Unbounded `decisions` DashMap in `Bouncer` Enables Memory Exhaustion via Unique-IP Flooding — (File: `rs/boundary_node/ic_boundary/src/bouncer/mod.rs`)

## Summary
The `Bouncer` struct maintains two per-IP maps: `shards` (rate-limiter buckets, bounded via `moka::sync::Cache` with `max_capacity`) and `decisions` (ban list, created as `DashMap::new()` with no capacity limit). Any IP that exhausts its burst is inserted into `decisions` with no eviction until `ban_time` expires. An attacker rotating through a large pool of unique source IPs can grow `decisions` without bound, exhausting process memory and OOM-killing the boundary node.

## Finding Description
`shards` is correctly bounded: [1](#0-0) 

`decisions` is created with no capacity limit: [2](#0-1) 

In `acquire_token`, every IP that exhausts its burst is unconditionally inserted into `decisions` with no size check: [3](#0-2) 

Entries are only removed by `process_releases`, which fires periodically and only evicts entries whose `ban_time` has elapsed: [4](#0-3) 

The check order in `acquire_token` is: `decisions` first (line 120), then `shards` (line 124). A fresh IP not in either map always creates a new shard entry, exhausts its burst, and lands in the unbounded `decisions`. The `bouncer_max_buckets` cap on `shards` provides no protection for `decisions`. [5](#0-4) 

## Impact Explanation
Each `Decision` entry is ~40 bytes of struct data plus DashMap overhead (~100–200 bytes per entry). At 1 million unique IPs: ~100–200 MB. At 10 million: ~1–2 GB. The boundary node process is OOM-killed, disabling all ingress protection — rate limiting, IP banning, and request routing. This matches the allowed High impact: **"Application/platform-level DoS, crash... or subnet availability impact not based on raw volumetric DDoS"** ($2,000–$10,000). The attack is not raw volumetric DDoS; it exploits a specific unbounded data structure with low per-IP traffic (burst_size+1 = 601 requests per IP by default).

## Likelihood Explanation
IPv6 makes this practical from a single machine. ISPs commonly assign /48 or /56 prefixes, providing 2^80 or 2^72 unique source addresses. Linux supports IPv6 source address selection from a prefix natively. The attacker needs only 601 requests per unique IP — not high bandwidth, just IP rotation. With IPv4, a modest botnet achieves the same effect. No special privileges are required; any external client can trigger this. The attack is repeatable and deterministic.

## Recommendation
Add a capacity cap to `decisions` consistent with how `shards` is bounded. The most robust fix is to replace `DashMap::new()` with a `moka::sync::Cache` configured with `max_capacity` tied to a new CLI parameter (e.g., `bouncer_max_decisions`, defaulting to a value like 1,000,000). Alternatively, enforce a hard cap in `acquire_token` by refusing to insert when `decisions.len()` exceeds a configured limit, dropping the ban insertion (fail-open for that IP) rather than allowing unbounded growth.

## Proof of Concept
```rust
#[test]
fn test_decisions_unbounded_growth() {
    use std::net::{IpAddr, Ipv6Addr};
    // burst_size=1 so each IP is banned after 2 requests
    let bouncer = Bouncer::new(1, 1, Duration::from_secs(600), 20000,
                               Duration::from_secs(30), fw, &Registry::new()).unwrap();
    for i in 0u128..1_000_000 {
        let ip = IpAddr::V6(Ipv6Addr::from(i));
        bouncer.acquire_token(ip); // allowed (fresh burst)
        bouncer.acquire_token(ip); // denied + inserted into decisions
    }
    assert_eq!(bouncer.decisions.len(), 1_000_000);
    // Process RSS grows ~200MB+ with no eviction until ban_time (600s) elapses
}
```
The `decisions` map will contain exactly 1,000,000 entries with no eviction, confirming unbounded growth. Scaling to 10M entries causes multi-GB RSS growth and eventual OOM.

### Citations

**File:** rs/boundary_node/ic_boundary/src/rate_limiting/sharded.rs (L29-32)
```rust
        let shards = Cache::builder()
            .time_to_idle(tti)
            .max_capacity(max_shards)
            .build();
```

**File:** rs/boundary_node/ic_boundary/src/bouncer/mod.rs (L104-104)
```rust
            decisions: DashMap::new(),
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

**File:** rs/boundary_node/ic_boundary/src/bouncer/mod.rs (L144-166)
```rust
    fn process_releases(&self, now: Instant) {
        // Collect IPs to be released
        let to_release = self
            .decisions
            .iter()
            .filter_map(|x| (now.duration_since(x.when) > x.length).then_some(x.ip))
            .collect::<Vec<_>>();

        if to_release.is_empty() {
            return;
        }

        info!("Bouncer: releasing {} IPs", to_release.len());
        debug!("Bouncer: releasing: {:?}", to_release);

        // Remove the released decisions & compact the map
        for ip in to_release {
            self.decisions.remove(&ip);
        }
        self.decisions.shrink_to_fit();

        self.mark_update();
    }
```

**File:** rs/boundary_node/ic_boundary/src/cli.rs (L417-420)
```rust
    /// Maximum number of IPs to track. This restricts memory usage to store buckets.
    /// If exceeded - old ones will be removed
    #[clap(env, long, default_value = "20000")]
    pub bouncer_max_buckets: u64,
```
