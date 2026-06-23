### Title
Node-Swap Kill Switch and Caller/Subnet Whitelists Are Permanently Bypassed in Production — (`rs/registry/canister/src/flags.rs`)

### Summary

The Registry canister's `do_swap_node_in_subnet_directly` endpoint applies three access-control checks — a global kill switch (`is_node_swapping_enabled`), a caller whitelist (`is_node_swapping_enabled_for_caller`), and a subnet whitelist (`is_node_swapping_enabled_on_subnet`) — but all three underlying flags are hardcoded to their permissive defaults in production and no production-reachable code path can ever change them. The checks exist and are called, but the state they read is immutable at runtime, making the entire phased-rollout access-control layer permanently inert.

### Finding Description

`rs/registry/canister/src/flags.rs` declares three `thread_local` statics:

```rust
static IS_NODE_SWAPPING_ENABLED: Cell<bool> = const { Cell::new(true) };
static NODE_SWAPPING_CALLERS_POLICY: RefCell<AccessList<PrincipalId>> =
    RefCell::new(AccessList::allow_all());
static NODE_SWAPPING_SUBNETS_POLICY: RefCell<AccessList<SubnetId>> =
    RefCell::new(AccessList::allow_all());
``` [1](#0-0) 

Every function that can mutate these statics is gated behind `#[cfg(test)]` or `#[cfg(any(test, feature = "test"))]`:

```rust
#[cfg(test)]
pub(crate) fn temporarily_disable_node_swapping() -> Temporary { … }

#[cfg(any(test, feature = "test"))]
pub mod temporary_overrides {
    pub fn test_set_swapping_status(…) { … }
    pub fn test_set_swapping_whitelisted_callers(…) { … }
    pub fn test_set_swapping_enabled_subnets(…) { … }
}
``` [2](#0-1) 

The `canister_init` function in `canister.rs` also calls these overrides, but only inside `#[cfg(feature = "test")]`:

```rust
#[cfg(feature = "test")]
{
    test_set_swapping_status(…);
    test_set_swapping_whitelisted_callers(…);
    test_set_swapping_enabled_subnets(…);
}
``` [3](#0-2) 

In the production binary (compiled without `feature = "test"`), none of these setters are compiled in. The three checks in `swap_nodes_inner` therefore always evaluate to their permissive defaults:

```rust
if !is_node_swapping_enabled() { … }   // always true → never fires
Self::swapping_enabled_for_caller(caller)?;  // allow_all → always Ok
Self::swapping_allowed_on_subnet(subnet_id)?; // allow_all → always Ok
``` [4](#0-3) 

The `RegistryCanisterInitPayload` fields `is_swapping_feature_enabled`, `swapping_whitelisted_callers`, and `swapping_enabled_subnets` are explicitly documented as integration-test-only and "shouldn't be provided when deploying to mainnet": [5](#0-4) 

### Impact Explanation

The intended design was a phased rollout with three independent safety levers:

1. **Global kill switch** — disable the entire feature if a bug is found.
2. **Caller whitelist** — restrict which node operators may use the feature.
3. **Subnet whitelist** — restrict which subnets may be targeted.

In production all three levers are permanently stuck in the "allow everything" position. Concretely:

- Any node operator (unprivileged ingress sender) can call `do_swap_node_in_subnet_directly` on the Registry canister without being whitelisted, on any subnet, at any time.
- If a bug is discovered in the node-swap logic (e.g., a validation bypass that lets an operator swap nodes they do not own, or a state-corruption path), the kill switch cannot be activated without a full canister upgrade and NNS governance vote — a process that takes days.
- The phased rollout guarantee documented in the code comments is entirely absent from the production binary.

### Likelihood Explanation

The entry path is direct: any node operator can send an ingress update to the Registry canister calling `swap_node_in_subnet_directly`. No special privilege, leaked key, or social engineering is required. The feature is already deployed to production (the `do_swap_node_in_subnet_directly` update method is exported from the canister). The remaining per-call validations (caller must own both nodes, rate limiting, subnet-halted check) are independent of the broken flags and do not compensate for the missing kill switch or whitelist.

### Recommendation

1. **Expose a privileged update method** (callable only by the NNS governance canister or the Registry canister's controllers) that sets `IS_NODE_SWAPPING_ENABLED`, `NODE_SWAPPING_CALLERS_POLICY`, and `NODE_SWAPPING_SUBNETS_POLICY` at runtime in production builds — analogous to how `migration_canister/src/privileged.rs` exposes `enable_api` / `disable_api` for its own kill switch.
2. Remove the `#[cfg(test)]` / `#[cfg(feature = "test")]` guards from the setter functions (or add production-safe wrappers) so the kill switch and whitelists are actually operable.
3. Until a runtime setter exists, default `IS_NODE_SWAPPING_ENABLED` to `false` and require an explicit governance proposal to enable the feature, matching the documented phased-rollout intent.

### Proof of Concept

1. Compile the Registry canister without `feature = "test"` (the normal production build).
2. Observe that `IS_NODE_SWAPPING_ENABLED.get()` always returns `true`, `NODE_SWAPPING_CALLERS_POLICY` always returns `allow_all`, and `NODE_SWAPPING_SUBNETS_POLICY` always returns `allow_all`.
3. Call `do_swap_node_in_subnet_directly` as any node operator who owns two nodes — the call succeeds regardless of whether that operator was ever "whitelisted" or whether the target subnet was ever "enabled", because the whitelist checks unconditionally pass.
4. Attempt to disable the feature by calling any canister method — no such method exists in the production ABI. The only recourse is a full NNS-governed canister upgrade. [6](#0-5) [7](#0-6) [8](#0-7)

### Citations

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

**File:** rs/registry/canister/src/flags.rs (L37-39)
```rust
pub(crate) fn is_node_swapping_enabled() -> bool {
    IS_NODE_SWAPPING_ENABLED.get()
}
```

**File:** rs/registry/canister/src/flags.rs (L41-82)
```rust
#[cfg(test)]
pub(crate) fn temporarily_disable_node_swapping() -> Temporary {
    Temporary::new(&IS_NODE_SWAPPING_ENABLED, false)
}

#[cfg(test)]
pub(crate) fn temporarily_enable_node_swapping() -> Temporary {
    Temporary::new(&IS_NODE_SWAPPING_ENABLED, true)
}

#[cfg(test)]
pub(crate) fn temporarily_enable_subnet_splitting() -> Temporary {
    Temporary::new(&IS_SUBNET_SPLITTING_ENABLED, true)
}

#[cfg(test)]
pub(crate) fn temporarily_disable_subnet_splitting() -> Temporary {
    Temporary::new(&IS_SUBNET_SPLITTING_ENABLED, false)
}

pub(crate) fn is_subnet_splitting_enabled() -> bool {
    IS_SUBNET_SPLITTING_ENABLED.get()
}

#[cfg(any(test, feature = "test"))]
pub mod temporary_overrides {
    use super::*;

    pub fn test_set_swapping_status(override_value: bool) {
        IS_NODE_SWAPPING_ENABLED.replace(override_value);
    }

    pub fn test_set_swapping_whitelisted_callers(override_callers: Vec<PrincipalId>) {
        let policy = AccessList::allow(override_callers);
        NODE_SWAPPING_CALLERS_POLICY.replace(policy);
    }

    pub fn test_set_swapping_enabled_subnets(override_subnets: Vec<SubnetId>) {
        let policy = AccessList::allow(override_subnets);
        NODE_SWAPPING_SUBNETS_POLICY.replace(policy);
    }
}
```

**File:** rs/registry/canister/src/flags.rs (L84-90)
```rust
pub(crate) fn is_node_swapping_enabled_on_subnet(subnet_id: SubnetId) -> bool {
    NODE_SWAPPING_SUBNETS_POLICY.with_borrow(|subnet_policy| subnet_policy.is_allowed(&subnet_id))
}

pub(crate) fn is_node_swapping_enabled_for_caller(caller: PrincipalId) -> bool {
    NODE_SWAPPING_CALLERS_POLICY.with_borrow(|caller_policy| caller_policy.is_allowed(&caller))
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

**File:** rs/registry/canister/src/mutations/do_swap_node_in_subnet_directly.rs (L110-147)
```rust
impl Registry {
    /// Called by the node operators in order to rotate their nodes without the need for governance.
    pub fn do_swap_node_in_subnet_directly(&mut self, payload: SwapNodeInSubnetDirectlyPayload) {
        self.swap_nodes_inner(payload, dfn_core::api::caller(), now_system_time())
            .unwrap_or_else(|e| panic!("{e}"));
    }

    /// Top level function for the swapping feature which has all inputs.
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
