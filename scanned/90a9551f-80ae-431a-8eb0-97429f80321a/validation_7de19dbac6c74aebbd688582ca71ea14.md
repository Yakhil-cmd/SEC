### Title
Node-Swapping Caller and Subnet Allowlists Default to `allow_all()`, Bypassing Phased-Rollout Access Control - (File: `rs/registry/canister/src/flags.rs`)

### Summary

In `rs/registry/canister/src/flags.rs`, the two access-control policies that gate the `do_swap_node_in_subnet_directly` registry mutation — `NODE_SWAPPING_CALLERS_POLICY` and `NODE_SWAPPING_SUBNETS_POLICY` — are initialized to `AccessList::allow_all()`. The code that would override these to a restrictive allowlist is compiled only under `#[cfg(feature = "test")]`, so on the production canister the policies remain fully permissive. Any node operator who owns the relevant nodes can therefore call `do_swap_node_in_subnet_directly` on any subnet, bypassing the intended phased-rollout whitelist entirely.

### Finding Description

**Root cause — permissive default state:**

`rs/registry/canister/src/flags.rs` lines 18–20:

```rust
static NODE_SWAPPING_CALLERS_POLICY: RefCell<AccessList<PrincipalId>>
    = RefCell::new(AccessList::allow_all());   // ← every caller passes

static NODE_SWAPPING_SUBNETS_POLICY: RefCell<AccessList<SubnetId>>
    = RefCell::new(AccessList::allow_all());   // ← every subnet passes
```

The comment on lines 13–17 explicitly states these flags exist for *"phased rollout … to specific subnets … to specific subset of callers."* The intent is a restrictive allowlist, but the default is the opposite.

**Why the init-time override never fires in production:**

`rs/registry/canister/canister/canister.rs` lines 208–237 show that the code which calls `test_set_swapping_whitelisted_callers` and `test_set_swapping_enabled_subnets` is wrapped in `#[cfg(feature = "test")]`. In a production build that block is dead code, so `NODE_SWAPPING_CALLERS_POLICY` and `NODE_SWAPPING_SUBNETS_POLICY` are never narrowed from `allow_all()`.

**Enforcement path in `do_swap_node_in_subnet_directly`:**

`rs/registry/canister/src/mutations/do_swap_node_in_subnet_directly.rs` lines 125–137:

```rust
if !is_node_swapping_enabled() { return Err(SwapError::FeatureDisabled); }
// ...
Self::swapping_enabled_for_caller(caller)?;   // queries NODE_SWAPPING_CALLERS_POLICY
// ...
Self::swapping_allowed_on_subnet(subnet_id)?; // queries NODE_SWAPPING_SUBNETS_POLICY
```

Because both policies are `allow_all()`, both checks always pass. The only remaining gate is `validate_node_swap`, which verifies the caller is the actual node operator of the nodes being swapped — a correct check, but not the phased-rollout restriction.

**`RegistryCanisterInitPayload` comment confirms the design intent:**

`rs/registry/canister/src/init.rs` lines 9–17 state these fields *"shouldn't be provided when deploying to mainnet and should be left behind the test configuration"* — meaning the production canister is expected to rely on the hardcoded defaults in `flags.rs`. Those defaults are `allow_all()`, which is the opposite of the intended restrictive rollout posture.

### Impact Explanation

Any node operator who legitimately owns two nodes (one assigned to a subnet, one unassigned) can call `do_swap_node_in_subnet_directly` on **any** subnet — including subnets that were not yet approved for the phased rollout. This allows premature use of a feature that may still carry unresolved bugs, and causes unplanned subnet membership changes. The rate limiter (1 swap per subnet per 4 hours, 1 per operator-subnet pair per 24 hours) reduces throughput but does not prevent the bypass. Subnet membership is a security-critical registry record: incorrect membership can degrade consensus fault tolerance.

### Likelihood Explanation

The attacker profile is a legitimate node operator — a role reachable without any privileged key. The call path (`do_swap_node_in_subnet_directly` → `swap_nodes_inner`) is a standard ingress update to the registry canister. No special preconditions beyond node ownership are required. The bug is present in every production deployment because the `#[cfg(feature = "test")]` guard permanently excludes the corrective init logic.

### Recommendation

Change the default initialization of both policies to `AccessList::deny_all()` so that no caller or subnet is permitted until explicitly whitelisted:

```rust
// rs/registry/canister/src/flags.rs
static NODE_SWAPPING_CALLERS_POLICY: RefCell<AccessList<PrincipalId>>
    = RefCell::new(AccessList::deny_all());  // safe default: no caller allowed

static NODE_SWAPPING_SUBNETS_POLICY: RefCell<AccessList<SubnetId>>
    = RefCell::new(AccessList::deny_all());  // safe default: no subnet allowed
```

Additionally, move the policy-override logic out of the `#[cfg(feature = "test")]` block so that production `canister_init` can also apply the init-payload values when provided.

### Proof of Concept

1. Node operator `A` owns `old_node` (assigned to subnet `S`, not in the phased-rollout whitelist) and `new_node` (unassigned).
2. `A` submits an ingress update to the registry canister calling `do_swap_node_in_subnet_directly { old_node_id: old_node, new_node_id: new_node }`.
3. `is_node_swapping_enabled()` returns `true` (default).
4. `swapping_enabled_for_caller(A)` queries `NODE_SWAPPING_CALLERS_POLICY` → `allow_all()` → returns `true`.
5. `swapping_allowed_on_subnet(S)` queries `NODE_SWAPPING_SUBNETS_POLICY` → `allow_all()` → returns `true`.
6. `validate_node_swap` passes because `A` is the legitimate operator of both nodes.
7. `swap_nodes_in_subnet` mutates the registry, replacing `old_node` with `new_node` in subnet `S` — a subnet that was never approved for the rollout. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** rs/registry/canister/src/flags.rs (L13-21)
```rust
    // Temporary flags related to the node swapping feature.
    //
    // These are needed for the phased rollout approach in order
    // allow granular rolling out of the feature to specific subnets
    // to specific subset of callers.
    static NODE_SWAPPING_CALLERS_POLICY: RefCell<AccessList<PrincipalId>> = RefCell::new(AccessList::allow_all());

    static NODE_SWAPPING_SUBNETS_POLICY: RefCell<AccessList<SubnetId>> = RefCell::new(AccessList::allow_all());
}
```

**File:** rs/registry/canister/canister/canister.rs (L208-237)
```rust
    #[cfg(feature = "test")]
    {
        use registry_canister::flags::temporary_overrides::{
            test_set_swapping_enabled_subnets, test_set_swapping_status,
            test_set_swapping_whitelisted_callers,
        };

        println!("{LOG_PREFIX}canister_init: Overriding swapping flags");
        println!(
            "{LOG_PREFIX}canister_intt: Swapping enabled: {:?}",
            init_payload.is_swapping_feature_enabled
        );
        test_set_swapping_status(init_payload.is_swapping_feature_enabled.unwrap_or_default());
        println!(
            "{LOG_PREFIX}canister_init: Swapping whietlisted callers: {:?}",
            init_payload.swapping_whitelisted_callers
        );
        test_set_swapping_whitelisted_callers(
            init_payload
                .swapping_whitelisted_callers
                .unwrap_or_default(),
        );
        println!(
            "{LOG_PREFIX}canister_init: Swapping enabled on subnets: {:?}",
            init_payload.swapping_enabled_subnets
        );
        test_set_swapping_enabled_subnets(
            init_payload.swapping_enabled_subnets.unwrap_or_default(),
        );
    }
```

**File:** rs/registry/canister/src/mutations/do_swap_node_in_subnet_directly.rs (L124-137)
```rust
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
```

**File:** rs/registry/canister/src/mutations/do_swap_node_in_subnet_directly.rs (L149-156)
```rust
    /// Check if the caller is whitelisted to use this feature.
    fn swapping_enabled_for_caller(caller: PrincipalId) -> Result<(), SwapError> {
        if !is_node_swapping_enabled_for_caller(caller) {
            return Err(SwapError::FeatureDisabledForCaller { caller });
        }

        Ok(())
    }
```

**File:** rs/registry/canister/src/mutations/do_swap_node_in_subnet_directly.rs (L169-175)
```rust
    fn swapping_allowed_on_subnet(subnet_id: SubnetId) -> Result<(), SwapError> {
        if !is_node_swapping_enabled_on_subnet(subnet_id) {
            return Err(SwapError::FeatureDisabledOnSubnet { subnet_id });
        }

        Ok(())
    }
```

**File:** rs/registry/canister/src/init.rs (L9-23)
```rust
    // IC-1869 (Node swaps) flags that are used
    // integration tests and will be removed as
    // a part of Phase 3 of the rollout.
    //
    // Note: in `src/flags.rs` are the default
    // values for all of these arguments and these
    // shouldn't be provided when deploying to
    // mainnet and should be left behind the
    // test configuration.
    //
    // Note: these flags are temporary and will
    // go away once the feature is fully deployed.
    pub is_swapping_feature_enabled: Option<bool>,
    pub swapping_whitelisted_callers: Option<Vec<PrincipalId>>,
    pub swapping_enabled_subnets: Option<Vec<SubnetId>>,
```
