### Title
Duplicate Positions in Firewall Rule Removal/Update Allow Silent Removal of Unintended Rules — (`File: rs/registry/canister/src/mutations/firewall.rs`)

### Summary
`remove_firewall_rules_compute_entries` and `update_firewall_rules_compute_entries` in the registry canister do not validate that the caller-supplied `positions` array contains unique indices. A governance participant can craft a `RemoveFirewallRules` or `UpdateFirewallRules` proposal with duplicate position values, causing the registry canister to silently remove or overwrite more firewall rules than the proposal appears to target, while still passing the `expected_hash` integrity check.

### Finding Description

`remove_firewall_rules_compute_entries` sorts the positions in descending order and calls `Vec::remove` for each:

```rust
pub fn remove_firewall_rules_compute_entries(
    current_entries: &mut Vec<FirewallRule>,
    payload: &RemoveFirewallRulesPayload,
) {
    let mut positions = payload.positions.clone();
    positions.sort_unstable();
    positions.reverse();
    for i in positions {
        current_entries.remove(i as usize);  // no duplicate check
    }
}
``` [1](#0-0) 

If `positions = [N, N]` is supplied, after sort+reverse the loop runs `remove(N)` twice. The first call removes the rule originally at index `N`. The second call removes whatever is now at index `N` — which was originally at index `N+1`. Two rules are removed instead of one, with no error or panic.

Similarly, `update_firewall_rules_compute_entries` iterates positions without a uniqueness check:

```rust
for (rule_idx, pos) in payload.positions.clone().into_iter().enumerate() {
    // no duplicate check
    current_entries[pos as usize] = payload.rules[rule_idx].clone();
}
``` [2](#0-1) 

With `positions = [N, N]` and `rules = [rule_A, rule_B]`, `rule_A` is written then immediately overwritten by `rule_B`. `rule_A` is silently discarded.

The `expected_hash` field is computed by the proposer over the **actual result** of the (buggy) operation. A proposer who knows the behavior can pre-compute the correct hash for the two-rule-removed result and submit it, causing the hash check in `do_set_firewall_rules` to pass: [3](#0-2) 

The registry canister exposes these as governance-gated update endpoints: [4](#0-3) 

### Impact Explanation

Firewall rules in the IC registry control which IP prefixes and ports are accessible on replica nodes, subnet nodes, and boundary nodes. Silently removing an extra rule (e.g., the rule that allows inter-node consensus traffic on port 4100, or the rule that allows HTTPS on port 8080) could disrupt subnet operation or expose nodes to unauthorized network access. The `expected_hash` mechanism is intended to let voters verify the outcome, but a proposal with `positions = [N, N]` superficially appears to remove one rule at position N (a redundant no-op), while actually removing two rules. Voters who do not carefully simulate the removal logic are deceived. [5](#0-4) 

### Likelihood Explanation

The attacker must be a governance participant (neuron holder) with enough voting power to pass a proposal, or must socially engineer other voters. This is a non-trivial barrier. However, the IC governance system is open — any principal can stake ICP and create a neuron. The deception is subtle: `positions = [2, 2]` looks like a redundant specification of the same position, not an attack. Voters relying on the `expected_hash` alone (without simulating the removal) would not detect the extra removal. Likelihood is **low-to-medium** given the governance barrier, but the attack surface is real for any sufficiently motivated neuron holder.

### Recommendation

Add a uniqueness check on `positions` before processing in both `remove_firewall_rules_compute_entries` and `update_firewall_rules_compute_entries`:

```rust
// In remove_firewall_rules_compute_entries:
let mut seen = std::collections::BTreeSet::new();
for &pos in &payload.positions {
    if !seen.insert(pos) {
        panic!("{}Duplicate position {} in RemoveFirewallRules payload.", LOG_PREFIX, pos);
    }
}

// In update_firewall_rules_compute_entries:
let mut seen = std::collections::BTreeSet::new();
for &pos in &payload.positions {
    if !seen.insert(pos) {
        panic!("{}Duplicate position {} in UpdateFirewallRules payload.", LOG_PREFIX, pos);
    }
}
``` [6](#0-5) 

### Proof of Concept

Assume a ruleset of 4 rules at indices 0, 1, 2, 3. A malicious proposer submits:

```
RemoveFirewallRulesPayload {
    scope: FirewallRulesScope::ReplicaNodes,
    positions: vec![2, 2],   // duplicate — appears to remove rule at index 2 only
    expected_hash: <hash of ruleset with rules 2 AND 3 removed>,
}
```

Execution in `remove_firewall_rules_compute_entries`:
1. `positions` after sort+reverse: `[2, 2]`
2. `current_entries.remove(2)` → removes rule originally at index 2; list is now length 3
3. `current_entries.remove(2)` → removes rule now at index 2 (originally index 3)
4. Result: two rules removed, not one

The `expected_hash` was pre-computed by the proposer over the 2-rule-removed result, so `do_set_firewall_rules` accepts it and commits the mutation to the registry. [7](#0-6)

### Citations

**File:** rs/registry/canister/src/mutations/firewall.rs (L26-32)
```rust
        // Compare hash
        let result_hash = compute_firewall_ruleset_hash(&rules);
        if result_hash != expected_hash {
            panic!(
                "{LOG_PREFIX}Provided expected hash for new firewall ruleset does not match. Expected hash: {expected_hash:?}, actual hash: {result_hash:?}."
            );
        }
```

**File:** rs/registry/canister/src/mutations/firewall.rs (L88-102)
```rust
    /// Remove firewall rules for a given scope.
    /// Removes the rules at the given positions.
    ///
    /// This method is called by the governance canister.
    pub fn do_remove_firewall_rules(&mut self, payload: RemoveFirewallRulesPayload) {
        println!(
            "{}do_remove_firewall_rules: scope: {:?}, positions: {:?}, expected_hash: {:?}",
            LOG_PREFIX, payload.scope, payload.positions, payload.expected_hash
        );

        let mut entries = self.fetch_current_ruleset(&payload.scope);
        remove_firewall_rules_compute_entries(&mut entries, &payload);

        self.do_set_firewall_rules(&payload.scope, entries, payload.expected_hash);
    }
```

**File:** rs/registry/canister/src/mutations/firewall.rs (L182-222)
```rust
pub fn remove_firewall_rules_compute_entries(
    current_entries: &mut Vec<FirewallRule>,
    payload: &RemoveFirewallRulesPayload,
) {
    // Remove entries from the back to front to preserve positions
    let mut positions = payload.positions.clone();
    positions.sort_unstable();
    positions.reverse();
    for i in positions {
        current_entries.remove(i as usize);
    }
}

/// Performs a firewall rules update. A rules update replaces existing rules in the given
/// ruleset in the payload, at the specified positions, with new given rules.
/// This function can be used both by the mutation code as well as by any testing code and
/// utilities such as ic-admin.
pub fn update_firewall_rules_compute_entries(
    current_entries: &mut [FirewallRule],
    payload: &UpdateFirewallRulesPayload,
) {
    if payload.positions.len() != payload.rules.len() {
        panic!(
            "{}Number of provided positions differs from number of provided rules. Positions: {:?}, Rules: {:?}.",
            LOG_PREFIX, payload.positions, payload.rules
        );
    }

    // Update the entries
    for (rule_idx, pos) in payload.positions.clone().into_iter().enumerate() {
        if pos < 0 || pos >= current_entries.len() as i32 {
            panic!(
                "{}Provided position is out of bounds for the existing ruleset. Position: {:?}, ruleset size: {:?}.",
                LOG_PREFIX,
                pos,
                current_entries.len()
            );
        }
        current_entries[pos as usize] = payload.rules[rule_idx].clone();
    }
}
```

**File:** rs/registry/canister/canister/canister.rs (L926-938)
```rust
#[unsafe(export_name = "canister_update remove_firewall_rules")]
fn remove_firewall_rules() {
    check_caller_is_governance_and_log("remove_firewall_rules");
    over(candid_one, |payload: RemoveFirewallRulesPayload| {
        remove_firewall_rules_(payload)
    });
}

#[candid_method(update, rename = "remove_firewall_rules")]
fn remove_firewall_rules_(payload: RemoveFirewallRulesPayload) {
    registry_mut().do_remove_firewall_rules(payload);
    recertify_registry();
}
```

**File:** rs/registry/canister/src/invariants/firewall.rs (L25-58)
```rust
/// Checks the firewall invariants:
///    * Principals refer to existing subnets and nodes
///    * A firewall rule is valid, iff:
///        * At least one IPv4 or IPv6 prefix is specified
///        * IP prefixes are valid (either v4 or v6, correct format)
///        * At least one port is specified
///        * Port numbers are valid (<= 65535)
///        * Action is allow or deny
///        * Comment is bounded in size (up to 255 characters)
pub(crate) fn check_firewall_invariants(
    snapshot: &RegistrySnapshot,
) -> Result<(), InvariantCheckError> {
    validate_firewall_rule_principals(snapshot)?;

    for node_id in get_all_node_records(snapshot).keys() {
        let node_ruleset = get_node_firewall_rules(snapshot, node_id);
        validate_firewall_ruleset(node_ruleset)?;
    }

    for subnet_id in get_subnet_ids_from_snapshot(snapshot) {
        let subnet_ruleset = get_subnet_firewall_rules(snapshot, &subnet_id);
        validate_firewall_ruleset(subnet_ruleset)?;
    }

    let replica_node_ruleset = get_replica_nodes_firewall_rules(snapshot);
    validate_firewall_ruleset(replica_node_ruleset)?;

    let boundary_node_ruleset = get_boundary_nodes_firewall_rules(snapshot);
    validate_firewall_ruleset(boundary_node_ruleset)?;

    let global_ruleset = get_global_firewall_rules(snapshot);
    validate_firewall_ruleset(global_ruleset)?;

    Ok(())
```
