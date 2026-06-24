The code is fully traced. Here is the complete analysis:

---

### Title
RestrictedRead Callers Can Enumerate Full Rule-Lifecycle History for Undisclosed Incidents via `get_rules_by_incident_id` — (`rs/boundary_node/rate_limits/canister/confidentiality_formatting.rs`, `getter.rs`, `add_config.rs`)

### Summary

A `RestrictedRead` (unprivileged) caller can reconstruct the complete addition/removal history of rate-limit rules for any incident — including confidential, undisclosed ones — by combining two query endpoints. The `RuleConfidentialityFormatter` redacts only `rule_raw` and `description`, leaving `added_in_version`, `removed_in_version`, and `rule_id` fully exposed. Simultaneously, `commit_changes` permanently accumulates rule IDs in `StorableIncident.rule_ids` and never prunes them on removal.

### Finding Description

**Step 1 — Incident ID discovery via `get_config`**

`get_config` is a `#[query]` method, so `inspect_message` does not apply to it. Any caller can invoke it. The `ConfigConfidentialityFormatter` only redacts `rule_raw` and `description` for non-disclosed rules; it leaves `rule_id` and `incident_id` fully visible: [1](#0-0) 

So a `RestrictedRead` caller calling `get_config` receives the `incident_id` UUID for every rule in the current config, including rules linked to confidential, undisclosed incidents.

**Step 2 — Historical rule enumeration via `get_rules_by_incident_id`**

`get_rules_by_incident_id` is also a `#[query]` method, bypassing `inspect_message` entirely: [2](#0-1) 

`IncidentGetter::get` iterates over `stored_incident.rule_ids` — which contains **all rules ever associated with the incident**, including removed ones — and for `RestrictedRead` callers applies `RuleConfidentialityFormatter::format`: [3](#0-2) 

`RuleConfidentialityFormatter::format` only redacts `rule_raw` and `description`. It does **not** redact `added_in_version`, `removed_in_version`, or `rule_id`: [4](#0-3) 

The `OutputRuleMetadata` returned to the caller therefore always includes:
- `rule_id` (UUID)
- `added_in_version` (version when rule was introduced)
- `removed_in_version` (version when rule was removed, if applicable) [5](#0-4) 

**Step 3 — `StorableIncident.rule_ids` is never pruned**

In `commit_changes`, when a rule is removed from a config, only `removed_in_version` is set on the `StorableRule`. The rule's ID is **never removed** from `StorableIncident.rule_ids`: [6](#0-5) 

The `extend` call only adds new rule IDs; it never removes old ones. Removed rules remain permanently in the incident's `rule_ids` set.

**Step 4 — Version-to-timestamp correlation**

The attacker can call `get_config(version)` for each version number to retrieve `active_since` timestamps, allowing full correlation of version numbers to wall-clock time.

### Impact Explanation

A `RestrictedRead` caller (any anonymous Internet Computer user) can:
1. Enumerate all incident IDs from `get_config` (incident IDs are not redacted)
2. For each incident, call `get_rules_by_incident_id` to retrieve the complete lifecycle of every rule ever associated with it — including rules added and subsequently removed across multiple config versions
3. Correlate `added_in_version`/`removed_in_version` with timestamps via `get_config` calls

This reveals: how many rules were created per incident, when each rule was introduced and retired, and the operational cadence of incident response — all for incidents that operators intended to keep confidential.

### Likelihood Explanation

The exploit requires no privileges, no keys, and no coordination. Both `get_config` and `get_rules_by_incident_id` are non-replicated query methods callable by any principal. The state-machine proof is trivial: add a rule to incident I in version V, omit it in version V+1, call `get_rules_by_incident_id(I)` as `RestrictedRead`, observe `removed_in_version = V+1` in the response.

### Recommendation

Two independent fixes are needed:

1. **In `RuleConfidentialityFormatter::format`**: For non-disclosed rules, also redact `added_in_version` and `removed_in_version` (set them to sentinel values or omit them).

2. **In `IncidentGetter::get`**: Before iterating `stored_incident.rule_ids`, check `stored_incident.is_disclosed`. If the incident is not disclosed and the caller is `RestrictedRead`, either return `NotFound` or return only the subset of rules that are individually disclosed.

Optionally, `ConfigConfidentialityFormatter::format` should also redact `incident_id` for non-disclosed rules to prevent incident ID harvesting at the `get_config` level.

### Proof of Concept

```
// State setup (privileged)
add_config([rule R linked to incident I])          // version V
add_config([])                                     // version V+1, rule R removed

// Attacker (RestrictedRead, any anonymous principal)
response = get_config(V)
// → rule R appears with incident_id = I (not redacted)

history = get_rules_by_incident_id(I)
// → returns OutputRuleMetadata {
//     rule_id: <uuid>,
//     incident_id: I,
//     rule_raw: None,          // redacted
//     description: None,       // redacted
//     added_in_version: V,     // NOT redacted ← leak
//     removed_in_version: V+1, // NOT redacted ← leak
//   }

timestamps_V   = get_config(V).active_since    // wall-clock time of V
timestamps_V1  = get_config(V+1).active_since  // wall-clock time of V+1
// Attacker now knows: rule was active from timestamps_V to timestamps_V1
```

### Citations

**File:** rs/boundary_node/rate_limits/canister/confidentiality_formatting.rs (L17-28)
```rust
    fn format(&self, config: OutputConfig) -> OutputConfig {
        let mut config = config;
        config.is_redacted = true;
        // Redact (hide) fields of non-disclosed rules
        config.rules.iter_mut().for_each(|rule| {
            if rule.disclosed_at.is_none() {
                rule.description = None;
                rule.rule_raw = None;
            }
        });
        config
    }
```

**File:** rs/boundary_node/rate_limits/canister/confidentiality_formatting.rs (L31-42)
```rust
impl ConfidentialityFormatting for RuleConfidentialityFormatter {
    type Input = OutputRuleMetadata;

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

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L136-146)
```rust
#[query]
fn get_rules_by_incident_id(incident_id: IncidentId) -> GetRulesByIncidentIdResponse {
    let caller_id = ic_cdk::api::caller();
    let response = with_canister_state(|state| {
        let access_resolver = AccessLevelResolver::new(caller_id, state.clone());
        let formatter = RuleConfidentialityFormatter;
        let getter = IncidentGetter::new(state, formatter, access_resolver);
        getter.get(&incident_id)
    })?;
    Ok(response)
}
```

**File:** rs/boundary_node/rate_limits/canister/getter.rs (L176-199)
```rust
        for rule_id in stored_incident.rule_ids.into_iter() {
            let stored_rule = self.canister_api.get_rule(&rule_id).ok_or_else(|| {
                // This error should never happen, it means that the stored data is inconsistent.
                GetEntityError::Internal(anyhow::anyhow!("Rule with id={rule_id} not found"))
            })?;

            let output_rule = OutputRuleMetadata {
                id: rule_id,
                incident_id,
                rule_raw: Some(stored_rule.rule_raw),
                description: Some(stored_rule.description),
                disclosed_at: stored_rule.disclosed_at,
                added_in_version: stored_rule.added_in_version,
                removed_in_version: stored_rule.removed_in_version,
            };

            if is_authorized_viewer {
                output_rules.push(output_rule.into());
            } else {
                // Hide non-disclosed rule from unauthorized viewers.
                let output_rule = self.formatter.format(output_rule);
                output_rules.push(output_rule.into());
            }
        }
```

**File:** rs/boundary_node/rate_limits/canister/add_config.rs (L219-232)
```rust
    for (incident_id, rule_ids) in incidents_map {
        let incident = canister_api
            .get_incident(&incident_id)
            .map(|mut stored_incident| {
                stored_incident.rule_ids.extend(rule_ids.clone());
                stored_incident
            })
            .unwrap_or_else(|| StorableIncident {
                is_disclosed: false,
                rule_ids: rule_ids.clone(),
            });

        canister_api.upsert_incident(incident_id, incident);
    }
```
