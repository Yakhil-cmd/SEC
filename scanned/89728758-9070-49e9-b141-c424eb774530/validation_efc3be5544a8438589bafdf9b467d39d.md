Looking at the exact code path and the `ipnet` crate's cross-family comparison semantics.

**Key code under review:**

`Bucket::evaluate` at line 140-144:
```rust
if let Some(v) = self.rule.ip
    && !v.contains(&ctx.ip)
{
    return None;
}
```

`ctx.ip` is populated at line 491:
```rust
ip: conn_info.remote_addr.ip(),
```

**Analysis:**

1. `self.rule.ip` is typed as `Option<IpNet>`. If configured as `10.0.0.0/24`, it is `IpNet::V4(...)`. [1](#0-0) 

2. `ctx.ip` is `IpAddr` taken directly from `conn_info.remote_addr.ip()` with **no canonicalization** (`to_canonical()` / `to_ipv4_mapped()` is never called). [2](#0-1) 

3. The `ipnet` crate's `IpNet::contains(&IpAddr)` performs **no cross-family matching**. `IpNet::V4(10.0.0.0/24).contains(IpAddr::V6(::ffff:10.0.0.1))` returns `false` unconditionally because the address families differ. This is documented behavior of the `ipnet` crate.

4. When the check returns `false`, the rule is skipped entirely (`return None`), meaning neither rate-limiting nor blocking is applied for that client. [3](#0-2) 

5. The `GenericLimiter::evaluate` loop falls through to `Decision::Pass` if no rule matches. [4](#0-3) 

**Precondition for exploitability:** The boundary node must present IPv4-mapped IPv6 addresses (`::ffff:x.x.x.x`) to the application layer. This occurs when the server binds to `::` (IPv6 wildcard) without `IPV6_V6ONLY`, which is the Linux default. The existing nftables configs show both IPv4 and IPv6 tables for the boundary node, consistent with dual-stack operation. [5](#0-4) 

The `ip_prefix_group` sharded path at lines 155-163 also branches on `IpAddr::V4` vs `IpAddr::V6` — an IPv4-mapped IPv6 address would take the `V6` branch and use `prefix.v6` for sharding, further diverging from the intended IPv4 rule behavior.

---

### Title
IPv4-mapped IPv6 addresses bypass IPv4 CIDR rate-limit and block rules in `Bucket::evaluate` — (`rs/boundary_node/ic_boundary/src/rate_limiting/generic.rs`)

### Summary
`Bucket::evaluate` compares `rule.ip` (an `IpNet`) against `ctx.ip` (an `IpAddr`) using `IpNet::contains`, which performs no cross-family normalization. A client connecting via an IPv4-mapped IPv6 address (`::ffff:10.0.0.1`) is never matched by an IPv4 CIDR rule (`10.0.0.0/24`), causing the rule to be silently skipped.

### Finding Description
At line 140–144 of `generic.rs`, the IP filter guard is:

```rust
if let Some(v) = self.rule.ip
    && !v.contains(&ctx.ip)
{
    return None;  // rule skipped
}
```

`ctx.ip` is set from `conn_info.remote_addr.ip()` without any call to `IpAddr::to_canonical()`. On a dual-stack Linux socket (bound to `::` without `IPV6_V6ONLY`), an IPv4 client's address is presented to userspace as `IpAddr::V6(::ffff:10.0.0.1)`. The `ipnet` crate's `IpNet::V4(10.0.0.0/24).contains(IpAddr::V6(::ffff:10.0.0.1))` returns `false`, so the rule is skipped and `Decision::Pass` is returned.

### Impact Explanation
Any operator-configured `ip`-scoped rule (rate-limit or block) targeting an IPv4 CIDR is completely ineffective against clients connecting via IPv4-mapped IPv6 addresses. This allows:
- Clients from a blocked IPv4 range to bypass the block entirely.
- Clients from a rate-limited IPv4 range to send unlimited requests.

### Likelihood Explanation
- The boundary node is a public-facing component accepting untrusted traffic.
- Dual-stack sockets without `IPV6_V6ONLY` are the Linux default.
- IPv4 CIDR rules are a natural operator configuration (e.g., blocking an abusive IPv4 range).
- No special privileges are required; any client can connect via IPv4-mapped IPv6.
- The bug is locally testable with a unit test.

### Recommendation
Normalize `ctx.ip` before comparison using `IpAddr::to_canonical()` (stabilized in Rust 1.75), which converts `::ffff:10.0.0.1` → `10.0.0.1`:

```rust
let ip = match ctx.ip {
    IpAddr::V6(v6) => v6.to_ipv4_mapped()
        .map(IpAddr::V4)
        .unwrap_or(ctx.ip),
    v4 => v4,
};

if let Some(v) = self.rule.ip
    && !v.contains(&ip)
{
    return None;
}
```

Apply the same normalization in the `ip_prefix_group` sharded branch.

### Proof of Concept

```rust
#[test]
fn test_ipv4_mapped_bypass() {
    use std::str::FromStr;
    use ipnet::IpNet;
    use std::net::IpAddr;

    let rule_ip: IpNet = "10.0.0.0/24".parse().unwrap();
    let client_ip: IpAddr = "::ffff:10.0.0.1".parse().unwrap();

    // This returns false — the rule is skipped for IPv4-mapped clients
    assert!(!rule_ip.contains(&client_ip),
        "IpNet::contains returns false for IPv4-mapped IPv6 vs IPv4 CIDR");

    // Bucket::evaluate returns None (rule skipped) → GenericLimiter returns Pass
    // A block or rate-limit rule for 10.0.0.0/24 is completely bypassed.
}
``` [3](#0-2) [2](#0-1)

### Citations

**File:** rs/boundary_node/rate_limits/api/src/schema_versions/v1.rs (L158-158)
```rust
    pub ip: Option<IpNet>,
```

**File:** rs/boundary_node/ic_boundary/src/rate_limiting/generic.rs (L140-144)
```rust
        if let Some(v) = self.rule.ip
            && !v.contains(&ctx.ip)
        {
            return None;
        }
```

**File:** rs/boundary_node/ic_boundary/src/rate_limiting/generic.rs (L155-163)
```rust
                Limiter::Sharded(v, prefix) => {
                    let prefix = match ctx.ip {
                        IpAddr::V4(_) => prefix.v4,
                        IpAddr::V6(_) => prefix.v6,
                    };

                    // We assume that the prefix is correct, assert is safe
                    let net = IpNet::new_assert(ctx.ip, prefix);
                    v.acquire(net)
```

**File:** rs/boundary_node/ic_boundary/src/rate_limiting/generic.rs (L403-410)
```rust
        for b in self.buckets.load_full().as_ref() {
            if let Some(v) = b.evaluate(&ctx) {
                return v;
            }
        }

        // No rules / no match -> pass
        Decision::Pass
```

**File:** rs/boundary_node/ic_boundary/src/rate_limiting/generic.rs (L486-492)
```rust
    let ctx = Context {
        subnet_id: subnet.id,
        canister_id: canister_id.map(|x| x.get().into()),
        method: ctx.method_name.as_deref(),
        request_type: ctx.request_type,
        ip: conn_info.remote_addr.ip(),
    };
```
