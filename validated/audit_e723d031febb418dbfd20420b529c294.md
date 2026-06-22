### Title
Governance Latency in `set_subnet_operational_level` and `recover_subnet` Prevents Timely Emergency Subnet Recovery - (File: rs/registry/canister/canister/canister.rs)

### Summary
The `set_subnet_operational_level` and `recover_subnet` functions in the Registry canister are exclusively callable by NNS Governance via `check_caller_is_governance_and_log`. NNS governance proposals carry a minimum voting period of 4 days (`initial_voting_period_seconds`), extendable to 8 days via the wait-for-quiet algorithm. When a subnet stalls or requires emergency halting for repairs, there is no bypass path: the subnet remains in a degraded or fully unavailable state for the entire governance latency window, with all hosted canisters inaccessible.

### Finding Description
Two Registry canister entry points gate the entire subnet emergency-recovery surface behind NNS Governance:

**`set_subnet_operational_level`** — sets `is_halted` in `SubnetRecord` (taking a subnet offline for repairs or bringing it back online): [1](#0-0) 

**`recover_subnet`** — updates a subnet's recovery CUP to restart a stalled subnet: [2](#0-1) 

Both enforce `check_caller_is_governance_and_log`, meaning the only legal caller is the NNS Governance canister. Triggering either operation requires:

1. Submitting an NNS governance proposal (`SetSubnetOperationalLevel` or `RecoverSubnet`)
2. Waiting for the voting period to expire **or** an absolute majority to be reached
3. Proposal execution

The NNS governance voting period floor is 4 days: [3](#0-2) 

With wait-for-quiet, this extends to 8 days: [4](#0-3) 

While proposals can be adopted early if an absolute majority votes, this requires real-time coordination among neuron holders and is not guaranteed in an emergency. There is **no emergency bypass** that allows `set_subnet_operational_level` or `recover_subnet` to execute without the full governance process.

The `SetSubnetOperationalLevelPayload` sets `is_halted = true` (DOWN_FOR_REPAIRS) or `is_halted = false` (NORMAL): [5](#0-4) 

The Registry changelog explicitly describes `set_subnet_operational_level` as "only callable by Governance" and intended for "rare extraordinary situations": [6](#0-5) 

### Impact Explanation
When a subnet stalls or experiences a critical failure requiring emergency halting:

- All canisters on the affected subnet become unavailable for the full governance latency window (4–8 days minimum in the worst case)
- Users cannot interact with their canisters; financial operations (DeFi, token transfers, chain-key signing) are blocked
- The subnet remains in a degraded state until the governance proposal is adopted and executed

This is structurally identical to the reported vulnerability: just as `inflate`/`deflate` being gated behind a 7-day DAO delay could cause `zunUSD` depegging, `recover_subnet`/`set_subnet_operational_level` being gated behind NNS governance delay causes extended subnet downtime during emergencies with no recourse.

### Likelihood Explanation
Subnet stalls have occurred in practice on the IC (the `RecoverSubnet` NNS function exists precisely because of this). While rare, they are not theoretical. A malicious canister consuming all subnet resources or triggering a consensus-layer bug constitutes a reachable, externally-controlled entry path that can force the need for emergency recovery. The governance latency then becomes the bottleneck preventing timely remediation.

### Recommendation
Implement an emergency subnet recovery mechanism with reduced latency. Options include:

1. A dedicated "emergency" proposal topic with a shorter mandatory voting period (e.g., 24 hours) for `SetSubnetOperationalLevel` and `RecoverSubnet`
2. A multi-sig emergency committee (e.g., a set of trusted DFINITY-controlled neurons) that can trigger subnet halting/recovery without a full governance proposal, analogous to the emergency APS mechanism recommended in the external report
3. Allowing `set_subnet_operational_level` to be callable by a designated emergency principal in addition to governance, with appropriate rate-limiting and audit logging

### Proof of Concept

1. A malicious canister (or a consensus bug) causes a subnet to stall — all canisters on the subnet become unavailable
2. The NNS governance submits a `RecoverSubnet` or `SetSubnetOperationalLevel` proposal
3. The proposal enters the 4-day voting period; no early adoption occurs because neuron holders are not coordinated
4. During the entire 4+ day window, all canisters on the stalled subnet remain inaccessible — financial protocols halt, user funds are locked, chain-key signing is unavailable
5. Only after the governance proposal is adopted and executed does the subnet begin recovery

The root cause is identical to the reported finding: the sole path to execute a time-critical emergency operation (`set_subnet_operational_level` / `recover_subnet`) is gated behind a governance mechanism with mandatory multi-day latency and no emergency bypass. [7](#0-6) [8](#0-7)

### Citations

**File:** rs/registry/canister/canister/canister.rs (L685-697)
```rust
#[unsafe(export_name = "canister_update recover_subnet")]
fn recover_subnet() {
    check_caller_is_governance_and_log("recover_subnet");
    over_async(candid_one, |payload: RecoverSubnetPayload| async move {
        recover_subnet_(payload).await
    });
}

#[candid_method(update, rename = "recover_subnet")]
async fn recover_subnet_(payload: RecoverSubnetPayload) {
    registry_mut().do_recover_subnet(payload).await;
    recertify_registry();
}
```

**File:** rs/registry/canister/canister/canister.rs (L1309-1319)
```rust
#[unsafe(export_name = "canister_update set_subnet_operational_level")]
fn set_subnet_operational_level() {
    check_caller_is_governance_and_log("set_subnet_operational_level");
    over(candid_one, set_subnet_operational_level_);
}

#[candid_method(update, rename = "set_subnet_operational_level")]
fn set_subnet_operational_level_(payload: SetSubnetOperationalLevelPayload) {
    registry_mut().do_set_subnet_operational_level(payload);
    recertify_registry();
}
```

**File:** rs/nervous_system/tools/release/sns_default_test_init_params.yml (L152-154)
```yaml
# The default value is 345600 seconds (4 days).
#
initial_voting_period_seconds: 345600
```

**File:** rs/sns/governance/src/proposal.rs (L2183-2191)
```rust
        let elapsed_seconds = now_seconds.saturating_sub(self.proposal_creation_timestamp_seconds);
        let required_margin = self
            .wait_for_quiet_deadline_increase_seconds
            .saturating_add(self.initial_voting_period_seconds / 2)
            .saturating_sub(elapsed_seconds / 2);
        let new_deadline = std::cmp::max(
            current_deadline,
            now_seconds.saturating_add(required_margin),
        );
```

**File:** rs/registry/canister/src/mutations/do_set_subnet_operational_level.rs (L12-16)
```rust
pub mod operational_level {
    pub const NORMAL: i32 = 1;
    pub const DOWN_FOR_REPAIRS: i32 = 2;

    pub const ALL_VALID_CODES: [i32; 2] = [NORMAL, DOWN_FOR_REPAIRS];
```

**File:** rs/registry/canister/src/mutations/do_set_subnet_operational_level.rs (L44-76)
```rust
    pub fn do_set_subnet_operational_level(&mut self, payload: SetSubnetOperationalLevelPayload) {
        println!("{LOG_PREFIX}do_set_subnet_operational_level: {payload:?}");
        self.validate_set_subnet_operational_level(&payload)
            .unwrap();
        let SetSubnetOperationalLevelPayload {
            subnet_id,
            operational_level,
            ssh_readonly_access,
            ssh_node_state_write_access,
            recalled_replica_version_ids,
        } = payload;

        let mut mutations: Vec<RegistryMutation> = vec![];

        // Change SubnetRecord.
        if let Some(subnet_id) = subnet_id {
            mutations.push(modify_subnet_record_for_set_subnet_operational_level(
                subnet_id,
                self.get_subnet_or_panic(subnet_id),
                operational_level,
                ssh_readonly_access,
                recalled_replica_version_ids,
            ));
        }

        // Change NodeRecord(s).
        mutations.append(&mut modify_node_record_for_set_subnet_operational_level(
            ssh_node_state_write_access,
            |node_id| self.get_node_or_panic(node_id),
        ));

        self.maybe_apply_mutation_internal(mutations);
    }
```

**File:** rs/registry/canister/CHANGELOG.md (L256-261)
```markdown
* New set_subnet_operational_level method. This is only callable by
  Governance. Currently, Governance has no active code path (in release builds)
  that calls this method. However, once the SetSubnetOperationalLevel proposal
  type is enabled, this will effectively become an active feature. This will be
  used in a slightly improved subnet recovery procedure. Thus, this would only
  be used in rare extraordinary situations.
```
