The key facts are now clear. Let me trace the full execution path precisely.

**Production defaults in `flags.rs`:**
- `IS_NODE_SWAPPING_ENABLED = true` [1](#0-0) 
- `NODE_SWAPPING_CALLERS_POLICY = AccessList::allow_all()` [2](#0-1) 
- `NODE_SWAPPING_SUBNETS_POLICY = AccessList::allow_all()` [3](#0-2) 

**`allow_all()` is `DenyOnly` with empty `HashSet`, so `is_allowed()` returns `true` for every input:** [4](#0-3) 

**`canister_init` only overrides these flags under `#[cfg(feature = "test")]`** — in a production (non-test) build, the `temporary_overrides` module does not exist and the init payload fields are silently ignored: [5](#0-4) 

**`validate_node_swap` has no check for system subnet type** — it only verifies node ownership, new node unassigned, and subnet not halted: [6](#0-5) 

---

### Title
Node Operator Can Swap NNS Subnet Membership Without Governance Approval Due to `allow_all()` Production Defaults — (`rs/registry/canister/src/flags.rs`)

### Summary
In the production (non-`test`-feature) build of the registry canister, `NODE_SWAPPING_CALLERS_POLICY` and `NODE_SWAPPING_SUBNETS_POLICY` are permanently initialized to `AccessList::allow_all()` with no runtime override mechanism. Combined with `IS_NODE_SWAPPING_ENABLED = true`, any node operator who legitimately owns nodes on any subnet — including the NNS subnet — can call `swap_node_in_subnet_directly` via ingress and mutate subnet membership without a governance proposal.

### Finding Description

`flags.rs` initializes both access-list policies to `allow_all()` (implemented as `DenyOnly` with an empty `HashSet`, so `is_allowed()` returns `true` for every principal and every subnet ID). [7](#0-6) 

The `temporary_overrides` module that would allow restricting these policies is gated behind `#[cfg(any(test, feature = "test"))]` and is therefore compiled out of the production binary. [8](#0-7) 

`canister_init` only reads the `swapping_whitelisted_callers` / `swapping_enabled_subnets` init-payload fields inside a `#[cfg(feature = "test")]` block, so even if an operator tried to deploy with a restrictive init payload, the production binary would ignore it and keep `allow_all()`. [5](#0-4) 

The `init.rs` comment acknowledges this is intentional for a "phased rollout" but explicitly states these fields "shouldn't be provided when deploying to mainnet" — meaning the production binary is expected to run with `allow_all()` for both policies. [9](#0-8) 

The execution path through `swap_nodes_inner` is:
1. `is_node_swapping_enabled()` → `true` (default)
2. `swapping_enabled_for_caller(caller)` → `Ok(())` (allow_all)
3. `swapping_allowed_on_subnet(subnet_id)` → `Ok(())` (allow_all)
4. Rate-limiter reservation (1 swap/subnet/4h, 1 swap/operator/subnet/24h)
5. `validate_node_swap` — checks node ownership and that the subnet is not halted, **no system-subnet type check**
6. `swap_nodes_in_subnet` — directly mutates the subnet membership record [10](#0-9) 

### Impact Explanation
A node operator who legitimately owns nodes on the NNS subnet can rotate their node out of the NNS subnet (and a replacement node in) without any governance proposal. This violates the core IC invariant that NNS subnet membership changes require an NNS governance vote. The NNS subnet runs the governance, registry, ledger, and root canisters; unauthorized membership changes can degrade its fault-tolerance properties and, over repeated swaps (rate-limited to 1/4h per subnet), progressively replace the honest node set.

### Likelihood Explanation
The precondition — owning nodes on the NNS subnet — is non-trivial but not impossible: NNS node operators are known, registered entities. The call is a standard ingress update to the public registry canister endpoint `swap_node_in_subnet_directly`. No key material beyond the node operator's own identity key is required. The feature is enabled by default in the production binary with no restriction mechanism available at runtime.

### Recommendation
1. Flip the production default for `IS_NODE_SWAPPING_ENABLED` to `false` until the phased rollout is complete, or
2. Add an explicit system-subnet guard in `validate_node_swap` that rejects swaps on subnets of type `SubnetType::System`, or
3. Move the policy-override mechanism out of `#[cfg(feature = "test")]` so that a restrictive allowlist can be configured at canister init time in production builds.

### Proof of Concept
```
// Attacker: legitimate node operator with principal `OP` owning
// old_node (on NNS subnet) and new_node (unassigned).
// Production registry canister (no "test" feature).

let payload = SwapNodeInSubnetDirectlyPayload {
    old_node_id: Some(old_node.get()),  // currently on NNS subnet
    new_node_id: Some(new_node.get()),  // unassigned, owned by OP
};

// Direct ingress update call — no governance proposal needed.
// IS_NODE_SWAPPING_ENABLED=true, both policies=allow_all() → all guards pass.
registry_canister.swap_node_in_subnet_directly(payload).await;
// NNS subnet membership record is now mutated.
``` [11](#0-10)

### Citations

**File:** rs/registry/canister/src/flags.rs (L11-11)
```rust
    static IS_NODE_SWAPPING_ENABLED: Cell<bool> = const { Cell::new(true) };
```

**File:** rs/registry/canister/src/flags.rs (L18-20)
```rust
    static NODE_SWAPPING_CALLERS_POLICY: RefCell<AccessList<PrincipalId>> = RefCell::new(AccessList::allow_all());

    static NODE_SWAPPING_SUBNETS_POLICY: RefCell<AccessList<SubnetId>> = RefCell::new(AccessList::allow_all());
```

**File:** rs/registry/canister/src/flags.rs (L65-82)
```rust
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

**File:** rs/nervous_system/access_list/src/lib.rs (L126-164)
```rust
    pub fn allow_all() -> Self {
        Self {
            inner: AccessListInner::DenyOnly(HashSet::new()),
        }
    }

    pub fn deny_all() -> Self {
        Self {
            inner: AccessListInner::AllowOnly(HashSet::new()),
        }
    }

    pub fn allow<I>(items: I) -> Self
    where
        I: IntoIterator<Item = T>,
    {
        let items: HashSet<T> = items.into_iter().collect();

        Self {
            inner: AccessListInner::AllowOnly(items),
        }
    }

    pub fn deny<I>(items: I) -> Self
    where
        I: IntoIterator<Item = T>,
    {
        let items: HashSet<T> = items.into_iter().collect();

        Self {
            inner: AccessListInner::DenyOnly(items),
        }
    }

    pub fn is_allowed(&self, item: &T) -> bool {
        match &self.inner {
            AccessListInner::AllowOnly(items) => items.contains(item),
            AccessListInner::DenyOnly(items) => !items.contains(item),
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

**File:** rs/registry/canister/src/mutations/do_swap_node_in_subnet_directly.rs (L112-115)
```rust
    pub fn do_swap_node_in_subnet_directly(&mut self, payload: SwapNodeInSubnetDirectlyPayload) {
        self.swap_nodes_inner(payload, dfn_core::api::caller(), now_system_time())
            .unwrap_or_else(|e| panic!("{e}"));
    }
```

**File:** rs/registry/canister/src/mutations/do_swap_node_in_subnet_directly.rs (L118-146)
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
```

**File:** rs/registry/canister/src/mutations/do_swap_node_in_subnet_directly.rs (L177-234)
```rust
    fn validate_node_swap(
        &self,
        old_node_id: PrincipalId,
        new_node_id: PrincipalId,
        caller: PrincipalId,
        subnet_id: SubnetId,
    ) -> Result<(), SwapError> {
        // Ensure that the nodes exist
        let old_node_id = NodeId::new(old_node_id);
        let new_node_id = NodeId::new(new_node_id);
        let old_node = self.get_node(old_node_id).ok_or(SwapError::UnknownNode {
            node_id: old_node_id.get(),
        })?;
        let new_node = self.get_node(new_node_id).ok_or(SwapError::UnknownNode {
            node_id: new_node_id.get(),
        })?;

        // Ensure that the old node is a member in a subnet
        // This is done before calling `validate_node_swap`

        // Ensure that the new node is not a member of any subnets
        let maybe_subnet_new_node =
            find_subnet_for_node(self, new_node_id, &self.get_subnet_list_record());

        if let Some(subnet_id) = maybe_subnet_new_node {
            return Err(SwapError::NewNodeAssigned {
                node_id: new_node_id.get(),
                subnet_id,
            });
        }

        // Ensure that both of the nodes are owned by the same node operator
        let old_node_operator = PrincipalId::try_from(old_node.node_operator_id).unwrap();
        let new_node_operator = PrincipalId::try_from(new_node.node_operator_id).unwrap();

        if old_node_operator != new_node_operator {
            return Err(SwapError::NodesOwnedByDifferentOperators);
        }

        // Ensure that the caller is the actual node operator of the nodes.
        // Since the before check passed we can check for either one of the
        // node operators, new or old.
        if new_node_operator != caller {
            return Err(SwapError::CallerNodeOperatorMismatch {
                caller,
                node_operator: new_node_operator,
            });
        }

        // Disalbe swapping of nodes during recovery, when the subnet
        // is halted.
        let subnet_record = self.get_subnet_or_panic(subnet_id);
        if subnet_record.is_halted {
            return Err(SwapError::SubnetHalted { subnet_id });
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
