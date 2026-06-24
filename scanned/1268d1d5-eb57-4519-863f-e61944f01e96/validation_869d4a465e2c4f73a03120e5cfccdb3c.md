The code is clear. Let me confirm the exact initialization behavior of the timer.

### Title
Stale In-Memory ACL Allows Decommissioned Boundary Node to Retrieve Salt During Polling Window — (`rs/boundary_node/salt_sharing/canister/helpers.rs`, `canister.rs`)

### Summary

The salt-sharing canister enforces access control using an in-memory `HashSet<Principal>` (`API_BOUNDARY_NODE_PRINCIPALS`) that is refreshed only periodically via a timer-driven inter-canister call to the registry. A principal removed from the registry retains salt access for up to `registry_polling_interval_secs` seconds because both the `inspect_message` gate and the `get_salt` body read from this stale set.

### Finding Description

`API_BOUNDARY_NODE_PRINCIPALS` is declared as a non-stable, in-memory `thread_local`: [1](#0-0) 

It is populated exclusively by `poll_api_boundary_nodes()`, which is scheduled as a repeating timer: [2](#0-1) 

The full replacement of the set happens only on a successful registry response: [3](#0-2) 

Both the pre-consensus gate and the actual handler read from this same set: [4](#0-3) [5](#0-4) 

`get_salt` is a `#[query]` method. Regular query calls bypass `inspect_message` entirely — only the in-body `is_api_boundary_node_principal` check applies. For update (replicated-query) calls, both gates use the same stale set. Either way, the stale window is identical.

### Impact Explanation

A decommissioned or compromised boundary node can call `get_salt` (as a query or replicated query) at any point within the `registry_polling_interval_secs` window after its registry removal. If the salt has rotated since the node's last legitimate fetch, the node obtains the new salt it should never have received. This enables continued anonymization-key misuse: the node can correlate or de-anonymize user traffic (IP addresses, etc.) using a salt it is no longer authorized to hold.

### Likelihood Explanation

The window is bounded but guaranteed to exist on every decommissioning event. The `registry_polling_interval_secs` parameter is operator-configured (60 s in integration tests). The exploit requires only that the attacker control the private key of the decommissioned node's principal — a realistic scenario for a compromised node being rotated out. No consensus corruption, threshold attack, or privileged operator access is needed; a single ingress or query message suffices.

### Recommendation

1. **Immediate revocation path**: expose a privileged `remove_api_boundary_node` update method that removes a single principal from `API_BOUNDARY_NODE_PRINCIPALS` synchronously, callable by governance or the canister controller, without waiting for the next poll cycle.
2. **Shorten the polling interval** as a defense-in-depth measure.
3. **Double-check on execution**: for the `get_salt` query path (which bypasses `inspect_message`), consider whether a certified-variable or replicated-query-only design would be more appropriate, so that the authorization check is always consensus-validated.

### Proof of Concept

PocketIc state-machine test outline:

1. Install the canister with `registry_polling_interval_secs = 60`.
2. Register node principal `P` in the mock registry; tick past the first poll interval so `API_BOUNDARY_NODE_PRINCIPALS` contains `P`.
3. Call `get_salt` as `P` (query) — assert `Ok(salt)`.
4. Remove `P` from the mock registry. **Do not tick past the next poll interval.**
5. Call `get_salt` as `P` again immediately — assert `Ok(salt)` is still returned (stale ACL).
6. Tick past `registry_polling_interval_secs` so `poll_api_boundary_nodes` fires.
7. Call `get_salt` as `P` — assert `Err(Unauthorized)`.

Step 5 demonstrates the invariant violation: a principal removed from the registry can still retrieve the salt before the next poll. [6](#0-5)

### Citations

**File:** rs/boundary_node/salt_sharing/canister/storage.rs (L51-51)
```rust
    pub static API_BOUNDARY_NODE_PRINCIPALS: RefCell<HashSet<Principal>> = RefCell::new(HashSet::new());
```

**File:** rs/boundary_node/salt_sharing/canister/helpers.rs (L33-34)
```rust
    let period = Duration::from_secs(init_arg.registry_polling_interval_secs);
    set_timer_interval(period, async || poll_api_boundary_nodes().await);
```

**File:** rs/boundary_node/salt_sharing/canister/helpers.rs (L88-91)
```rust
        Ok((Ok(api_bn_records),)) => {
            // Set authorized readers of salt.
            let principals: HashSet<_> = api_bn_records.into_iter().filter_map(|n| n.id).collect();
            API_BOUNDARY_NODE_PRINCIPALS.with(|cell| *cell.borrow_mut() = principals);
```

**File:** rs/boundary_node/salt_sharing/canister/helpers.rs (L125-127)
```rust
pub fn is_api_boundary_node_principal(principal: &Principal) -> bool {
    API_BOUNDARY_NODE_PRINCIPALS.with(|cell| cell.borrow().contains(principal))
}
```

**File:** rs/boundary_node/salt_sharing/canister/canister.rs (L25-26)
```rust
    if called_method == REPLICATED_QUERY_METHOD && is_api_boundary_node_principal(&caller_id) {
        accept_message();
```

**File:** rs/boundary_node/salt_sharing/canister/canister.rs (L53-65)
```rust
fn get_salt() -> GetSaltResponse {
    let caller_id = caller();
    if is_api_boundary_node_principal(&caller_id) {
        let stored_salt = SALT
            .with(|cell| cell.borrow().get(&()))
            .ok_or(GetSaltError::SaltNotInitialized)?;

        return Ok(SaltResponse {
            salt: stored_salt.salt,
            salt_id: stored_salt.salt_id,
        });
    }
    Err(GetSaltError::Unauthorized)
```
