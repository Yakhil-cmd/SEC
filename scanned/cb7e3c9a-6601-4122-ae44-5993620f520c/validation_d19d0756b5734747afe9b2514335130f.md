### Title
NNS Governance `manage_neuron` Configure Operations Produce No Audit Trail Despite Existing Infrastructure - (File: `rs/nns/governance/src/neuron/types.rs`, `rs/nns/governance/src/audit_event.rs`)

---

### Summary

The NNS Governance canister has a stable-memory audit event log (`StableLog<AuditEvent>`) and a dedicated `add_audit_event` function, but that function is marked `#[allow(dead_code)]` and is never called for any user-triggered neuron configuration operation. All `manage_neuron` `Configure` operations — including `AddHotKey`, `RemoveHotKey`, `JoinCommunityFund`, `LeaveCommunityFund`, `ChangeAutoStakeMaturity`, and `SetVisibility` — mutate important neuron state without emitting any audit event. There is no on-chain record of when hot keys (which carry full governance voting power) were added to or removed from neurons.

---

### Finding Description

The NNS Governance canister defines a stable-memory audit log: [1](#0-0) 

The `add_audit_event` helper is the only write path into that log: [2](#0-1) 

The `#[allow(dead_code)]` attribute on line 19 is the key signal: this function is **never called** in any production code path. A `grep` across `rs/nns/governance/src/**/*.rs` confirms the only file that defines `add_audit_event` is `audit_event.rs` itself — no caller exists.

The `AuditEvent` proto only covers three internal system-migration events: [3](#0-2) 

Meanwhile, the `configure` function — the handler for all user-triggered neuron configuration — mutates neuron state across ten operation variants with no audit emission: [4](#0-3) 

Specifically, `AddHotKey` and `RemoveHotKey` are processed at lines 875–889 with no call to `add_audit_event`. The `add_hot_key` primitive simply pushes to a `Vec`: [5](#0-4) 

The SNS Governance canister has an `upgrade_journal` but it covers only upgrade lifecycle events, not neuron permission changes. No general audit event system exists for SNS neuron operations either. [6](#0-5) 

---

### Impact Explanation

Hot keys in NNS Governance carry full voting power on behalf of a neuron — they can vote on proposals that govern the entire Internet Computer network (subnet upgrades, node provider rewards, registry mutations, etc.). Because no audit event is emitted when a hot key is added or removed:

1. There is no on-chain record of which principals held hot-key access to a neuron at any historical point in time.
2. If a hot key is compromised, used to vote on critical proposals, and then silently removed, there is no forensic trail in the audit log.
3. Observers, dashboards, and governance tooling cannot reconstruct the history of neuron access delegation from the audit log alone — they can only see the current state by querying the neuron directly.

This is directly analogous to the Futureswap finding: the most important missing events are those for non-iterable access-control structures. Hot keys are stored per-neuron in a `Vec<PrincipalId>` — the current set is readable, but the history is permanently lost.

---

### Likelihood Explanation

Any neuron controller (an unprivileged ingress sender) can call `manage_neuron` with `Configure { operation: AddHotKey { new_hot_key: <any principal> } }` at any time. This is a standard, publicly documented operation. The missing audit emission happens on every successful call. Likelihood is **high** — this is not a corner case; it is the normal operational path for every neuron that uses hot keys.

---

### Recommendation

1. Add `AddHotKey`, `RemoveHotKey`, `JoinCommunityFund`, `LeaveCommunityFund`, `ChangeAutoStakeMaturity`, and `SetVisibility` variants to the `AuditEvent` proto in `rs/nns/governance/proto/ic_nns_governance/pb/v1/governance.proto`.
2. Call `add_audit_event(...)` at the end of each successful branch in the `configure` function in `rs/nns/governance/src/neuron/types.rs`.
3. Remove the `#[allow(dead_code)]` suppression from `add_audit_event` in `rs/nns/governance/src/audit_event.rs` so the compiler enforces that the function is actually used.
4. Consider adding equivalent audit events to SNS Governance neuron permission operations (`AddNeuronPermissions`, `RemoveNeuronPermissions`).

---

### Proof of Concept

**Entry path** (unprivileged ingress):

```
Caller: any neuron controller principal
Canister: NNS Governance (rwlgt-iiaaa-aaaaa-aaaaa-cai)
Method: manage_neuron
Payload:
  ManageNeuron {
    neuron_id_or_subaccount: NeuronId { id: <neuron_id> },
    command: Configure {
      operation: AddHotKey {
        new_hot_key: <attacker_or_delegate_principal>
      }
    }
  }
```

**Execution path**:

1. `manage_neuron` → `configure` in `rs/nns/governance/src/neuron/types.rs:812`
2. `Operation::AddHotKey` branch at line 875 → `self.add_hot_key(hot_key)` at line 882
3. `add_hot_key` pushes to `self.hot_keys` at line 674 — **no `add_audit_event` call anywhere in this path**
4. The `StableLog<AuditEvent>` in stable memory is untouched; the change is invisible to any audit log consumer

**Verification**: `grep -r "add_audit_event" rs/nns/governance/src/` returns only the definition in `audit_event.rs` — zero call sites in production code. [7](#0-6) [8](#0-7) [9](#0-8)

### Citations

**File:** rs/nns/governance/src/storage.rs (L17-19)
```rust
const UPGRADES_MEMORY_ID: MemoryId = MemoryId::new(0);
const AUDIT_EVENTS_INDEX_MEMORY_ID: MemoryId = MemoryId::new(1);
const AUDIT_EVENTS_DATA_MEMORY_ID: MemoryId = MemoryId::new(2);
```

**File:** rs/nns/governance/src/storage.rs (L88-89)
```rust
    // Events for audit purposes.
    audit_events_log: StableLog<AuditEvent, VM, VM>,
```

**File:** rs/nns/governance/src/audit_event.rs (L19-24)
```rust
#[allow(dead_code)]
pub fn add_audit_event(event: AuditEvent) {
    with_audit_events_log(|log| {
        log.append(&event).expect("failed to append an event");
    });
}
```

**File:** rs/nns/governance/proto/ic_nns_governance/pb/v1/governance.proto (L2611-2623)
```text
// Audit events in order to leave an audit trail for certain operations.
message AuditEvent {
  // The timestamp of the event.
  uint64 timestamp_seconds = 1;

  oneof payload {
    // Reset aging timestamps (https://forum.dfinity.org/t/icp-neuron-age-is-52-years/21261/26).
    ResetAging reset_aging = 2;
    // Restore aging timestamp that were incorrectly reset (https://forum.dfinity.org/t/restore-neuron-age-in-proposal-129394/29840).
    RestoreAging restore_aging = 3;
    // Normalize neuron dissolve state and age (https://forum.dfinity.org/t/simplify-neuron-state-age/30527)
    NormalizeDissolveStateAndAge normalize_dissolve_state_and_age = 4;
  }
```

**File:** rs/nns/governance/src/neuron/types.rs (L657-675)
```rust
    fn add_hot_key(&mut self, new_hot_key: &PrincipalId) -> Result<(), GovernanceError> {
        // Make sure that the same hot key is not added twice.
        for key in &self.hot_keys {
            if *key == *new_hot_key {
                return Err(GovernanceError::new_with_message(
                    ErrorType::HotKey,
                    "Hot key duplicated.",
                ));
            }
        }
        // Allow at most 10 hot keys per neuron.
        if self.hot_keys.len() >= MAX_NUM_HOT_KEYS_PER_NEURON {
            return Err(GovernanceError::new_with_message(
                ErrorType::ResourceExhausted,
                "Reached the maximum number of hotkeys.",
            ));
        }
        self.hot_keys.push(*new_hot_key);
        Ok(())
```

**File:** rs/nns/governance/src/neuron/types.rs (L812-904)
```rust
    pub fn configure(
        &mut self,
        caller: &PrincipalId,
        now_seconds: u64,
        cmd: &Configure,
    ) -> Result<(), GovernanceError> {
        let op = &cmd.operation.as_ref().ok_or_else(|| {
            GovernanceError::new_with_message(
                ErrorType::InvalidCommand,
                "Configure must have an operation.",
            )
        })?;

        self.is_authorized_to_configure_or_err(caller, op)?;

        match op {
            Operation::IncreaseDissolveDelay(d) => {
                if d.additional_dissolve_delay_seconds == 0 {
                    return Err(GovernanceError::new_with_message(
                        ErrorType::InvalidCommand,
                        "Additional delay is 0.",
                    ));
                }
                self.increase_dissolve_delay(now_seconds, d.additional_dissolve_delay_seconds);
                Ok(())
            }
            Operation::SetDissolveTimestamp(d) => {
                if now_seconds > d.dissolve_timestamp_seconds {
                    return Err(GovernanceError::new_with_message(
                        ErrorType::InvalidCommand,
                        "The dissolve delay must be set to a future time.",
                    ));
                }
                let desired_dd = d.dissolve_timestamp_seconds.saturating_sub(now_seconds);
                let current_dd = self.dissolve_delay_seconds(now_seconds);

                if current_dd > desired_dd {
                    return Err(GovernanceError::new_with_message(
                        ErrorType::InvalidCommand,
                        "Can't set a dissolve delay that is smaller than the current dissolve delay.",
                    ));
                }

                let dd_diff = desired_dd.saturating_sub(current_dd);
                if dd_diff == 0 {
                    return Err(GovernanceError::new_with_message(
                        ErrorType::InvalidCommand,
                        "Additional delay is 0.",
                    ));
                }
                self.increase_dissolve_delay(
                    now_seconds,
                    dd_diff.try_into().map_err(|_| {
                        GovernanceError::new_with_message(
                            ErrorType::InvalidCommand,
                            "Can't convert u64 dissolve delay into u32.",
                        )
                    })?,
                );
                Ok(())
            }
            Operation::StartDissolving(_) => self.start_dissolving(now_seconds),
            Operation::StopDissolving(_) => self.stop_dissolving(now_seconds),
            Operation::AddHotKey(k) => {
                let hot_key = k.new_hot_key.as_ref().ok_or_else(|| {
                    GovernanceError::new_with_message(
                    ErrorType::InvalidCommand,
                    "Operation AddHotKey requires the hot key to add to be specified in the input",
                )
                })?;
                self.add_hot_key(hot_key)
            }
            Operation::RemoveHotKey(k) => {
                let hot_key = k.hot_key_to_remove.as_ref().ok_or_else(|| GovernanceError::new_with_message(
                    ErrorType::InvalidCommand,
                    "Operation RemoveHotKey requires the hot key to remove to be specified in the input",
                ))?;
                self.remove_hot_key(hot_key)
            }
            Operation::JoinCommunityFund(_) => self.join_community_fund(now_seconds),
            Operation::LeaveCommunityFund(_) => self.leave_community_fund(),
            Operation::ChangeAutoStakeMaturity(change) => {
                if change.requested_setting_for_auto_stake_maturity {
                    self.auto_stake_maturity = Some(true);
                } else {
                    self.auto_stake_maturity = None;
                }
                Ok(())
            }
            Operation::SetVisibility(set_visibility) => {
                self.set_visibility(set_visibility.visibility)
            }
        }
```

**File:** rs/sns/governance/src/upgrade_journal.rs (L117-137)
```rust
impl Governance {
    pub fn push_to_upgrade_journal<Event>(&mut self, event: Event)
    where
        upgrade_journal_entry::Event: From<Event>,
    {
        let event = upgrade_journal_entry::Event::from(event);
        let upgrade_journal_entry = UpgradeJournalEntry {
            event: Some(event),
            timestamp_seconds: Some(self.env.now()),
        };
        match self.proto.upgrade_journal {
            None => {
                self.proto.upgrade_journal = Some(UpgradeJournal {
                    entries: vec![upgrade_journal_entry],
                });
            }
            Some(ref mut journal) => {
                journal.entries.push(upgrade_journal_entry);
            }
        }
    }
```
