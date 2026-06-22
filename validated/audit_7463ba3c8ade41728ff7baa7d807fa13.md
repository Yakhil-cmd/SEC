### Title
Open-by-Default Node Swapping Bypasses Phased-Rollout Whitelist — (`rs/registry/canister/src/flags.rs`)

### Summary

The production defaults for `NODE_SWAPPING_CALLERS_POLICY` and `NODE_SWAPPING_SUBNETS_POLICY` are both `AccessList::allow_all()`, and `IS_NODE_SWAPPING_ENABLED` defaults to `true`. This means any node operator who owns two nodes (one assigned to a subnet, one unassigned) can call `swap_node_in_subnet_directly` without appearing on any explicit whitelist — directly contradicting the stated phased-rollout design documented in the code.

### Finding Description

In `rs/registry/canister/src/flags.rs`, the two access-control thread-locals are initialized as:

```rust
static NODE_SWAPPING_CALLERS_POLICY: RefCell<AccessList<PrincipalId>> =
    RefCell::new(AccessList::allow_all());

static NODE_SWAPPING_SUBNETS_POLICY: RefCell<AccessList<SubnetId>> =
    RefCell::new(AccessList::allow_all());
``` [1](#0-0) 

The comment immediately above these declarations states they exist for a **phased rollout** to "specific subnets" and "specific subset of callers": [2](#0-1) 

`AccessList::allow_all()` is implemented as `DenyOnly(HashSet::new())`: [3](#0-2) 

And `is_allowed()` on a `DenyOnly` variant returns `!items.contains(item)` — with an empty set, this is always `true` for every caller and every subnet: [4](#0-3) 

The guard functions `swapping_enabled_for_caller` and `swapping_allowed_on_subnet` in `swap_nodes_inner` both delegate to these policies: [5](#0-4) 

So in production, with no explicit configuration, both checks unconditionally pass for every caller and every subnet.

### Impact Explanation

Any registered node operator who owns:
- one node currently assigned to a subnet, and
- one unassigned node

can call `do_swap_node_in_subnet_directly` and substitute their node in any subnet they participate in, without any governance proposal or explicit whitelist entry. The only remaining guards are:
- ownership check (caller must own both nodes) — `validate_node_swap`
- subnet-halted check
- rate limits (1 swap per 4 h per subnet, 1 per 24 h per operator per subnet) [6](#0-5) 

This allows a node operator to rotate in a degraded, misconfigured, or adversarially-controlled node into a subnet without governance approval, potentially affecting subnet liveness or security.

### Likelihood Explanation

The path is fully reachable via a standard ingress call to the registry canister's `do_swap_node_in_subnet_directly` endpoint. No privileged key, governance majority, or threshold corruption is required — only node ownership, which is a normal operational state for any registered node provider. [7](#0-6) 

### Recommendation

Change the production defaults to `AccessList::deny_all()` (or equivalently `AccessList::allow([])`) for both `NODE_SWAPPING_CALLERS_POLICY` and `NODE_SWAPPING_SUBNETS_POLICY`, consistent with the stated phased-rollout intent. Explicit whitelisting should be required before any caller or subnet can use the feature. The `temporary_overrides` module already provides the mechanism to add entries during rollout. [8](#0-7) 

### Proof of Concept

A unit test with no flag overrides (pure production defaults) would:
1. Create a registry with a node operator owning two nodes (one in a subnet, one unassigned).
2. Call `swap_nodes_inner` with the operator as caller.
3. Observe `Ok(())` — no whitelist entry for the caller or subnet was ever set.

The existing test `valid_payload_test` actually demonstrates the inverse: it explicitly calls `test_set_swapping_whitelisted_callers(vec![])` to override the default to `DenyAll` before asserting `FeatureDisabledForCaller`, confirming that without that override the default would have allowed the call. [9](#0-8)

### Citations

**File:** rs/registry/canister/src/flags.rs (L13-17)
```rust
    // Temporary flags related to the node swapping feature.
    //
    // These are needed for the phased rollout approach in order
    // allow granular rolling out of the feature to specific subnets
    // to specific subset of callers.
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

**File:** rs/nervous_system/access_list/src/lib.rs (L126-130)
```rust
    pub fn allow_all() -> Self {
        Self {
            inner: AccessListInner::DenyOnly(HashSet::new()),
        }
    }
```

**File:** rs/nervous_system/access_list/src/lib.rs (L160-165)
```rust
    pub fn is_allowed(&self, item: &T) -> bool {
        match &self.inner {
            AccessListInner::AllowOnly(items) => items.contains(item),
            AccessListInner::DenyOnly(items) => !items.contains(item),
        }
    }
```

**File:** rs/registry/canister/src/mutations/do_swap_node_in_subnet_directly.rs (L112-115)
```rust
    pub fn do_swap_node_in_subnet_directly(&mut self, payload: SwapNodeInSubnetDirectlyPayload) {
        self.swap_nodes_inner(payload, dfn_core::api::caller(), now_system_time())
            .unwrap_or_else(|e| panic!("{e}"));
    }
```

**File:** rs/registry/canister/src/mutations/do_swap_node_in_subnet_directly.rs (L135-137)
```rust
        Self::swapping_enabled_for_caller(caller)?;
        let subnet_id = self.find_subnet_for_old_node(old_node_id)?;
        Self::swapping_allowed_on_subnet(subnet_id)?;
```

**File:** rs/registry/canister/src/mutations/do_swap_node_in_subnet_directly.rs (L177-233)
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
```

**File:** rs/registry/canister/src/mutations/do_swap_node_in_subnet_directly.rs (L479-497)
```rust
    #[test]
    fn valid_payload_test() {
        let mut registry = Registry::new();

        let _temp = temporarily_enable_node_swapping();
        test_set_swapping_whitelisted_callers(vec![]);
        test_set_swapping_enabled_subnets(vec![]);

        let payload = valid_payload();

        let result =
            registry.swap_nodes_inner(payload, PrincipalId::new_user_test_id(1), now_system_time());

        // First error that occurs after validation
        assert!(result.is_err_and(|err| err
            == SwapError::FeatureDisabledForCaller {
                caller: PrincipalId::new_user_test_id(1)
            }));
    }
```
