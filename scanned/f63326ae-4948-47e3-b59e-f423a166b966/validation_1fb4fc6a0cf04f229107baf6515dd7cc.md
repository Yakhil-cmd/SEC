### Title
Stale `API_BOUNDARY_NODE_PRINCIPALS` Grants Indefinite `FullRead` Access to Decommissioned Boundary Nodes on Registry Poll Failure — (`rs/boundary_node/rate_limits/canister/canister.rs`, `access_control.rs`, `storage.rs`)

---

### Summary

When `periodically_poll_api_boundary_nodes` fails (registry canister unreachable or returns an error), the in-memory `API_BOUNDARY_NODE_PRINCIPALS` `HashSet` is never cleared. A decommissioned API boundary node whose principal remains in the stale set retains `FullRead` access indefinitely, receiving unredacted `rule_raw` and `description` for all undisclosed rate-limit rules.

---

### Finding Description

`API_BOUNDARY_NODE_PRINCIPALS` is declared as a heap-only `thread_local!` `RefCell<HashSet<Principal>>`, initialized empty and populated only on a successful registry poll: [1](#0-0) 

`set_api_boundary_nodes_principals` performs a full replacement of the set — but only when the call succeeds: [2](#0-1) 

In `periodically_poll_api_boundary_nodes`, both failure branches (`Ok(Err(...))` and `Err(...)`) only log and update metrics. Neither branch clears or modifies the `HashSet`: [3](#0-2) 

`AccessLevelResolver::get_access_level` reads directly from this potentially stale set: [4](#0-3) 

`RuleGetter::get` (and `ConfigGetter::get`, `IncidentGetter::get`) bypass the `RuleConfidentialityFormatter` entirely for any caller with `FullRead` or `FullAccess`: [5](#0-4) 

The formatter — which would otherwise redact `rule_raw` and `description` for undisclosed rules — is never invoked for `FullRead` callers: [6](#0-5) 

---

### Impact Explanation

A decommissioned (or compromised) API boundary node operator retains indefinite read access to confidential, undisclosed rate-limit rules (`rule_raw`, `description`) for as long as the registry poll continues to fail. This leaks active incident countermeasures — the exact patterns being rate-limited — to a principal that should have had its access revoked.

---

### Likelihood Explanation

Registry poll failures are realistic: network partitions, registry canister upgrades, or transient inter-canister call rejections all trigger the failure path. The polling interval is configurable; during any sustained failure window the stale set is never corrected. The decommissioned node operator retains their private key and can issue query calls at any time.

---

### Recommendation

On any poll failure, either:
1. Clear `API_BOUNDARY_NODE_PRINCIPALS` (fail-closed: revoke all `FullRead` access until the next successful poll), or
2. Record a `last_successful_poll_timestamp` and treat the set as expired after a configurable staleness threshold, falling back to `RestrictedRead` for all principals when the set is stale.

Option 1 is simpler and fail-safe. Option 2 preserves availability for boundary nodes during brief registry outages while still enforcing revocation after a bounded window.

---

### Proof of Concept

State-machine test outline:

1. Install the rate-limit canister with a mocked registry that returns a boundary node principal `P`.
2. Trigger one successful poll — `P` is added to `API_BOUNDARY_NODE_PRINCIPALS`.
3. Add an undisclosed rule (no `disclosed_at`).
4. Switch the mock registry to return an error on all subsequent calls.
5. Remove `P` from the registry mock's node list (simulating decommissioning).
6. Advance the timer to trigger several poll cycles — all fail, `HashSet` unchanged.
7. Call `get_rule_by_id` as principal `P`.
8. Assert the response contains non-`None` `rule_raw` and `description` — confirming `FullRead` was granted despite decommissioning.

The relevant code path: `canister.rs::get_rule_by_id` → `AccessLevelResolver::get_access_level` checks stale `API_BOUNDARY_NODE_PRINCIPALS` [4](#0-3) 
→ returns `FullRead` → `RuleGetter::get` skips formatter [7](#0-6) 
→ returns unredacted rule.

### Citations

**File:** rs/boundary_node/rate_limits/canister/storage.rs (L149-149)
```rust
    pub static API_BOUNDARY_NODE_PRINCIPALS: RefCell<HashSet<Principal>> = RefCell::new(HashSet::new());
```

**File:** rs/boundary_node/rate_limits/canister/state.rs (L130-133)
```rust
    fn set_api_boundary_nodes_principals(&self, principals: Vec<Principal>) {
        API_BOUNDARY_NODE_PRINCIPALS
            .with(|cell| *cell.borrow_mut() = HashSet::from_iter(principals));
    }
```

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L285-299)
```rust
                Ok((Err(err),)) => {
                    log!(
                        P0,
                        "[poll_api_boundary_nodes]: failed to fetch nodes from registry {err:?}",
                    );
                    ("failure", "calling_canister_method_failed")
                }
                Err(err) => {
                    log!(
                        P0,
                        "[poll_api_boundary_nodes]: failed to fetch nodes from registry {err:?}",
                    );
                    ("failure", "canister_call_rejected")
                }
            };
```

**File:** rs/boundary_node/rate_limits/canister/access_control.rs (L46-52)
```rust
        let has_full_read_access = self
            .canister_api
            .is_api_boundary_node_principal(&self.caller_id);

        if has_full_read_access {
            return AccessLevel::FullRead;
        }
```

**File:** rs/boundary_node/rate_limits/canister/getter.rs (L234-245)
```rust
        let is_authorized_viewer = self.access_resolver.get_access_level()
            == AccessLevel::FullAccess
            || self.access_resolver.get_access_level() == AccessLevel::FullRead;

        if is_authorized_viewer {
            return Ok(output_rule.into());
        }

        // Hide non-disclosed rules from unauthorized viewers.
        let output_rule = self.formatter.format(output_rule);

        Ok(output_rule.into())
```

**File:** rs/boundary_node/rate_limits/canister/confidentiality_formatting.rs (L34-42)
```rust
    fn format(&self, rule: OutputRuleMetadata) -> OutputRuleMetadata {
        let mut rule = rule;
        // Redact (hide) fields of non-disclosed rule
        if rule.disclosed_at.is_none() {
            rule.description = None;
            rule.rule_raw = None;
        }
        rule
    }
```
