### Title
In-Memory Rate Limiter and Access-Control Flags for `swap_node_in_subnet_directly` Reset on Canister Upgrade, Allowing Unlimited Node Swaps - (File: rs/registry/canister/src/mutations/do_swap_node_in_subnet_directly.rs)

### Summary

The `swap_node_in_subnet_directly` endpoint in the Registry canister has no governance-level access control at the canister entry point. Its only protections — a per-subnet/per-operator rate limiter and caller/subnet whitelist policies — are stored exclusively in `thread_local!` heap variables. Because IC canister upgrades replace the Wasm heap with fresh initial values, all three protection layers reset to their permissive defaults after every upgrade: the global feature flag resets to `true`, both access-control policies reset to `allow_all()`, and the rate limiter resets to zero usage. Any whitelisted (or, after upgrade, any) node operator can then perform unlimited subnet membership swaps until the canister is manually re-configured.

### Finding Description

**Permissionless canister entry point.** The canister handler for `swap_node_in_subnet_directly` contains no `check_caller_is_governance_and_log` guard — unlike the majority of registry mutation endpoints: [1](#0-0) 

Compare with a governed endpoint: [2](#0-1) 

**All protection state lives in `thread_local!` heap variables.** Three independent guards are declared as thread-locals: [3](#0-2) [4](#0-3) 

**Defaults are permissive.** `IS_NODE_SWAPPING_ENABLED` initialises to `true`; both `NODE_SWAPPING_CALLERS_POLICY` and `NODE_SWAPPING_SUBNETS_POLICY` initialise to `AccessList::allow_all()`. The `SWAP_LIMITER` initialises with zero committed usage (full capacity available).

**`allow_all()` when whitelist is empty.** The test suite itself documents this behaviour:

```
// In order to avoid the feature being enabled for all node
// operators there needs to be some other caller whitelisted.
builder.whitelist_swapping_feature_caller(PrincipalId::new_user_test_id(999));
``` [5](#0-4) 

**Rate limiter is in-memory only.** `SWAP_LIMITER` uses `InMemoryRateLimiter`, not the stable-memory-backed `RateLimiter` used elsewhere (e.g., `NODE_PROVIDER_RATE_LIMITER`): [6](#0-5) [7](#0-6) 

**Execution path after upgrade.** After any registry canister upgrade, `swap_nodes_inner` sees `is_node_swapping_enabled() == true`, `is_node_swapping_enabled_for_caller(any) == true`, `is_node_swapping_enabled_on_subnet(any) == true`, and a fresh rate limiter: [8](#0-7) 

The only remaining check is that the caller must be the node operator of both nodes — a check the node operator trivially satisfies for their own nodes.

### Impact Explanation

A node operator who owns nodes on a subnet can, immediately after any registry canister upgrade:

1. Perform unlimited node swaps on any subnet (rate limits cleared, subnet whitelist reset to `allow_all()`).
2. Rapidly cycle nodes in and out of subnets — including NNS or signing subnets — without governance approval, potentially destabilising consensus or threshold-signature availability.
3. Game node-provider reward calculations by swapping in high-uptime nodes just before reward snapshots and swapping them back out afterward, earning rewards they would not otherwise qualify for.

This is a direct analog to M-10: a function that should be access-controlled (or at minimum persistently rate-limited) is effectively uncontrolled after every upgrade, and the caller has a financial incentive to exploit it in ways that harm the network.

### Likelihood Explanation

The registry canister is upgraded regularly as part of NNS governance proposals. Every upgrade resets the protections. The exploit requires only that the attacker be a registered node operator (a permissioned but not rare role) and that the feature flag was enabled before the upgrade. The window of exposure begins immediately after the upgrade completes and lasts until the NNS manually re-configures the flags via a subsequent proposal.

### Recommendation

1. **Persist the swapping flags and rate-limiter state in stable memory** (or re-apply them from a stored `RegistryCanisterInitPayload` in `post_upgrade`), mirroring the pattern used by `NODE_PROVIDER_RATE_LIMITER` and `NODE_OPERATOR_RATE_LIMITER`.
2. **Change the `AccessList` default from `allow_all()` to `allow_none()`** so that an empty whitelist denies all callers rather than permitting all callers.
3. **Add a `check_caller_is_governance_and_log` guard** at the canister entry point, or at minimum ensure the feature-enabled flag defaults to `false` and is re-applied on every upgrade.

### Proof of Concept

1. NNS governance enables `swap_node_in_subnet_directly` for node operator `A` on subnet `S` and sets a rate limit of 1 swap per 4 hours.
2. Node operator `A` performs one swap, consuming the rate limit.
3. NNS governance passes a routine registry canister upgrade proposal.
4. Immediately after the upgrade, `IS_NODE_SWAPPING_ENABLED == true`, `NODE_SWAPPING_CALLERS_POLICY == allow_all()`, `NODE_SWAPPING_SUBNETS_POLICY == allow_all()`, and `SWAP_LIMITER` has zero committed usage.
5. Node operator `A` (and any other node operator) can now call `swap_node_in_subnet_directly` on any subnet without restriction, bypassing the intended 4-hour rate limit and the caller/subnet whitelist entirely.

### Citations

**File:** rs/registry/canister/canister/canister.rs (L795-801)
```rust
#[unsafe(export_name = "canister_update update_node_operator_config")]
fn update_node_operator_config() {
    check_caller_is_governance_and_log("update_node_operator_config");
    over(candid_one, |payload: UpdateNodeOperatorConfigPayload| {
        update_node_operator_config_(payload)
    });
}
```

**File:** rs/registry/canister/canister/canister.rs (L831-842)
```rust
#[unsafe(export_name = "canister_update swap_node_in_subnet_directly")]
fn swap_node_in_subnet_directly() {
    over(candid_one, |payload: SwapNodeInSubnetDirectlyPayload| {
        swap_node_in_subnet_directly_(payload)
    });
}

#[candid_method(update, rename = "swap_node_in_subnet_directly")]
fn swap_node_in_subnet_directly_(payload: SwapNodeInSubnetDirectlyPayload) {
    registry_mut().do_swap_node_in_subnet_directly(payload);
    recertify_registry();
}
```

**File:** rs/registry/canister/src/flags.rs (L8-21)
```rust
thread_local! {
    static IS_SUBNET_SPLITTING_ENABLED: Cell<bool> = const { Cell::new(false) };
    static IS_CHUNKIFYING_LARGE_VALUES_ENABLED: Cell<bool> = const { Cell::new(true) };
    static IS_NODE_SWAPPING_ENABLED: Cell<bool> = const { Cell::new(true) };

    // Temporary flags related to the node swapping feature.
    //
    // These are needed for the phased rollout approach in order
    // allow granular rolling out of the feature to specific subnets
    // to specific subset of callers.
    static NODE_SWAPPING_CALLERS_POLICY: RefCell<AccessList<PrincipalId>> = RefCell::new(AccessList::allow_all());

    static NODE_SWAPPING_SUBNETS_POLICY: RefCell<AccessList<SubnetId>> = RefCell::new(AccessList::allow_all());
}
```

**File:** rs/registry/canister/src/mutations/do_swap_node_in_subnet_directly.rs (L43-58)
```rust
impl SwapRateLimiter {
    fn new() -> Self {
        Self {
            subnet_limiter: InMemoryRateLimiter::new_in_memory(RateLimiterConfig {
                add_capacity_amount: 1,
                add_capacity_interval: NODE_SWAPS_SUBNET_CAPACITY_INTERVAL,
                max_capacity: 1,
                max_reservations: 1,
            }),
            node_operator_limiter: InMemoryRateLimiter::new_in_memory(RateLimiterConfig {
                add_capacity_amount: 1,
                add_capacity_interval: NODE_SWAPS_NODE_OPERATOR_CAPACITY_INTERVAL,
                max_capacity: 1,
                max_reservations: 1,
            }),
        }
```

**File:** rs/registry/canister/src/mutations/do_swap_node_in_subnet_directly.rs (L106-108)
```rust
thread_local! {
    static SWAP_LIMITER: RefCell<SwapRateLimiter> = RefCell::new(SwapRateLimiter::new());
}
```

**File:** rs/registry/canister/src/mutations/do_swap_node_in_subnet_directly.rs (L118-147)
```rust
    fn swap_nodes_inner(
        &mut self,
        payload: SwapNodeInSubnetDirectlyPayload,
        caller: PrincipalId,
        now: SystemTime,
    ) -> Result<(), SwapError> {
        // Check if the feature is enabled on the network.
        if !is_node_swapping_enabled() {
            return Err(SwapError::FeatureDisabled);
        }

        // Check if the payload is valid by itself.
        payload.validate()?;
        let (old_node_id, new_node_id) =
            (payload.old_node_id.unwrap(), payload.new_node_id.unwrap());

        //Check if the feature is allowed on the target subnet and for the caller
        Self::swapping_enabled_for_caller(caller)?;
        let subnet_id = self.find_subnet_for_old_node(old_node_id)?;
        Self::swapping_allowed_on_subnet(subnet_id)?;

        let reservation =
            SWAP_LIMITER.with_borrow_mut(|limiter| limiter.try_reserve(caller, subnet_id, now))?;

        self.validate_node_swap(old_node_id, new_node_id, caller, subnet_id)?;
        self.swap_nodes_in_subnet(subnet_id, old_node_id, new_node_id)?;

        SWAP_LIMITER.with_borrow_mut(|limiter| limiter.commit(reservation, now));
        Ok(())
    }
```

**File:** rs/registry/canister/tests/swap_node_in_subnet_directly.rs (L169-171)
```rust
    // In order to avoid the feature being enabled for all node
    // operators there needs to be some other caller whitelisted.
    builder.whitelist_swapping_feature_caller(PrincipalId::new_user_test_id(999));
```

**File:** rs/registry/canister/src/rate_limits.rs (L33-56)
```rust
thread_local! {
    static NODE_PROVIDER_RATE_LIMITER: RefCell<
        RateLimiter<String, StableMemoryCapacityStorage<String, VM>>,
    > = RefCell::new(RateLimiter::new_stable(
        RateLimiterConfig {
            add_capacity_amount: 1,
            add_capacity_interval: Duration::from_secs(NODE_PROVIDER_CAPACITY_ADD_INTERVAL_SECONDS),
            max_capacity: NODE_PROVIDER_MAX_SPIKE,
            max_reservations: NODE_PROVIDER_MAX_SPIKE * 2,
        },
        get_node_provider_rate_limiter_memory(),
    ));

    static NODE_OPERATOR_RATE_LIMITER: RefCell<
        RateLimiter<String, StableMemoryCapacityStorage<String, VM>>,
    > = RefCell::new(RateLimiter::new_stable(
        RateLimiterConfig {
            add_capacity_amount: 1,
            add_capacity_interval: Duration::from_secs(NODE_OPERATOR_CAPACITY_ADD_INTERVAL_SECONDS),
            max_capacity: NODE_OPERATOR_MAX_SPIKE,
            max_reservations: NODE_OPERATOR_MAX_SPIKE * 2,
        },
        get_node_operator_rate_limiter_memory(),
    ));
```
