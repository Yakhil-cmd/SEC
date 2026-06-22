### Title
Shared Subnet-Level Rate Limiter in `do_swap_node_in_subnet_directly` Allows One Whitelisted Operator to DoS Another's Swap - (`rs/registry/canister/src/mutations/do_swap_node_in_subnet_directly.rs`)

---

### Summary

The `do_swap_node_in_subnet_directly` endpoint uses a **shared, per-subnet** in-memory rate limiter (`SWAP_LIMITER`) that caps swaps to 1 per subnet per 4 hours across **all** node operators. Any whitelisted node operator who performs a legitimate swap for subnet S consumes that subnet's rate-limit slot, causing every other whitelisted operator's concurrent swap attempt on the same subnet to fail with `SubnetRateLimited` for the next 4 hours. This is a direct ordering-dependent DoS: whichever operator's message is ordered first wins; the other is locked out.

---

### Finding Description

`do_swap_node_in_subnet_directly` calls `swap_nodes_inner`, which reserves capacity from a `thread_local` `SWAP_LIMITER` before validating or applying the swap:

```rust
// rs/registry/canister/src/mutations/do_swap_node_in_subnet_directly.rs
let reservation =
    SWAP_LIMITER.with_borrow_mut(|limiter| limiter.try_reserve(caller, subnet_id, now))?;

self.validate_node_swap(old_node_id, new_node_id, caller, subnet_id)?;
self.swap_nodes_in_subnet(subnet_id, old_node_id, new_node_id)?;

SWAP_LIMITER.with_borrow_mut(|limiter| limiter.commit(reservation, now));
``` [1](#0-0) 

The `SwapRateLimiter` contains two limiters:

```rust
struct SwapRateLimiter {
    subnet_limiter: InMemoryRateLimiter<SubnetId>,          // 1 per subnet per 4 hours
    node_operator_limiter: InMemoryRateLimiter<(PrincipalId, SubnetId)>, // 1 per (op, subnet) per 24h
}
``` [2](#0-1) 

The `subnet_limiter` key is **only `SubnetId`** — it is shared across all operators. The `try_reserve` call for the subnet limiter uses `subnet_id` alone:

```rust
let subnet_reservation =
    self.subnet_limiter
        .try_reserve(now, subnet_id, 1)
        ...
``` [3](#0-2) 

The rate-limit configuration confirms max capacity of 1 per 4-hour interval:

```rust
subnet_limiter: InMemoryRateLimiter::new_in_memory(RateLimiterConfig {
    add_capacity_amount: 1,
    add_capacity_interval: NODE_SWAPS_SUBNET_CAPACITY_INTERVAL,
    max_capacity: 1,
    max_reservations: 1,
}),
``` [4](#0-3) 

The existing test explicitly confirms that a **different** operator is also blocked after the first operator consumes the slot:

```rust
// Call from a different node operator should fail as well
let response = swap_limiter
    .try_reserve(caller_2, subnet_id, before_duration_elapsed)
    .expect_err("Should error out");
assert_eq!(response, expected_err); // SubnetRateLimited
``` [5](#0-4) 

The `SWAP_LIMITER` is stored as a `thread_local`, meaning it is **in-memory only** and not persisted to stable storage. It resets on canister upgrade, but between upgrades it enforces the 4-hour subnet-wide lock. [6](#0-5) 

---

### Impact Explanation

Two whitelisted node operators A and B both have nodes in subnet S and both submit `swap_node_in_subnet_directly` calls. Whichever message is ordered first by the IC consensus layer succeeds; the other receives `SubnetRateLimited` and cannot retry for 4 hours. In a time-sensitive operational scenario — e.g., a degraded node that must be rotated out urgently — operator A can intentionally frontrun operator B's swap to block it. The resulting state (which node was swapped) depends entirely on message ordering, which neither operator controls. The 4-hour lockout is the direct impact; in a subnet recovery or urgent maintenance scenario this delay is operationally significant.

---

### Likelihood Explanation

The `swap_node_in_subnet_directly` feature is actively deployed and available to all whitelisted node operators on enabled subnets. Large subnets have multiple node operators. Any two operators independently deciding to rotate a node in the same subnet within the same 4-hour window — even without malicious intent — will trigger this ordering dependency. Malicious frontrunning requires only that operator A monitor the IC mempool (or simply submit a swap call preemptively) to block operator B.

---

### Recommendation

Replace the shared `SubnetId`-keyed subnet limiter with a per-`(PrincipalId, SubnetId)` key, so each operator's rate limit is independent. If a global per-subnet cap is still desired for safety, it should be enforced separately from the per-operator limit and should not cause one operator's legitimate action to block another's. Additionally, document the ordering dependency so operators are aware that concurrent swap submissions for the same subnet are not guaranteed to both succeed.

---

### Proof of Concept

1. Subnet S is enabled for swapping; operators A and B are both whitelisted.
2. Operator A owns nodes `A_old` (in subnet S) and `A_new` (unassigned).
3. Operator B owns nodes `B_old` (in subnet S) and `B_new` (unassigned).
4. Both operators submit `swap_node_in_subnet_directly` calls at approximately the same time.
5. The IC consensus layer orders A's message before B's.
6. A's call: `try_reserve(caller=A, subnet_id=S, now)` → succeeds; subnet slot consumed.
7. B's call: `try_reserve(caller=B, subnet_id=S, now)` → `SubnetRateLimited { subnet_id: S }` → panics.
8. Operator B's node `B_old` remains in subnet S; operator B cannot retry for 4 hours.
9. If operator A acts maliciously, they can monitor for B's pending call and preemptively submit their own swap to guarantee B is locked out.

### Citations

**File:** rs/registry/canister/src/mutations/do_swap_node_in_subnet_directly.rs (L32-35)
```rust
struct SwapRateLimiter {
    subnet_limiter: InMemoryRateLimiter<SubnetId>,
    node_operator_limiter: InMemoryRateLimiter<(PrincipalId, SubnetId)>,
}
```

**File:** rs/registry/canister/src/mutations/do_swap_node_in_subnet_directly.rs (L46-51)
```rust
            subnet_limiter: InMemoryRateLimiter::new_in_memory(RateLimiterConfig {
                add_capacity_amount: 1,
                add_capacity_interval: NODE_SWAPS_SUBNET_CAPACITY_INTERVAL,
                max_capacity: 1,
                max_reservations: 1,
            }),
```

**File:** rs/registry/canister/src/mutations/do_swap_node_in_subnet_directly.rs (L67-75)
```rust
        let subnet_reservation =
            self.subnet_limiter
                .try_reserve(now, subnet_id, 1)
                .map_err(|e| match e {
                    ic_nervous_system_rate_limits::RateLimiterError::NotEnoughCapacity => {
                        SwapError::SubnetRateLimited { subnet_id }
                    }
                    re => panic!("Unexpected error from subnet rate limiter: {re:?}"),
                })?;
```

**File:** rs/registry/canister/src/mutations/do_swap_node_in_subnet_directly.rs (L106-108)
```rust
thread_local! {
    static SWAP_LIMITER: RefCell<SwapRateLimiter> = RefCell::new(SwapRateLimiter::new());
}
```

**File:** rs/registry/canister/src/mutations/do_swap_node_in_subnet_directly.rs (L139-145)
```rust
        let reservation =
            SWAP_LIMITER.with_borrow_mut(|limiter| limiter.try_reserve(caller, subnet_id, now))?;

        self.validate_node_swap(old_node_id, new_node_id, caller, subnet_id)?;
        self.swap_nodes_in_subnet(subnet_id, old_node_id, new_node_id)?;

        SWAP_LIMITER.with_borrow_mut(|limiter| limiter.commit(reservation, now));
```

**File:** rs/registry/canister/src/mutations/do_swap_node_in_subnet_directly.rs (L738-742)
```rust
        // Call from a different node operator should fail as well
        let response = swap_limiter
            .try_reserve(caller_2, subnet_id, before_duration_elapsed)
            .expect_err("Should error out");
        assert_eq!(response, expected_err);
```
