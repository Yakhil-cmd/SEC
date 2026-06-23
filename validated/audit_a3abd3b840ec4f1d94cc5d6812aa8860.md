### Title
SNS Governance `disburse_maturity` Missing Neuron Lock Check Allows Maturity Extraction During Locked Async Operations - (File: rs/sns/governance/src/governance.rs)

### Summary

The SNS governance `disburse_maturity()` function does not check `in_flight_commands` before mutating neuron state, while the analogous NNS `disburse_maturity()` and other SNS neuron operations (e.g., `disburse_neuron`) all acquire a neuron lock first. This allows a caller with `DisburseMaturity` permission to deduct maturity from a neuron that is currently locked by an in-progress async ledger operation, violating the lock invariant and potentially causing maturity to be permanently lost.

### Finding Description

The NNS governance `disburse_maturity()` explicitly acquires a `SyncCommand` lock before calling `initiate_maturity_disbursement()`:

```rust
// rs/nns/governance/src/governance.rs ~line 3102-3118
let in_flight_command = NeuronInFlightCommand {
    timestamp: now_seconds,
    command: Some(InFlightCommand::SyncCommand(SyncCommand {})),
};
let _neuron_lock = self.lock_neuron_for_command(id.id, in_flight_command)?;
initiate_maturity_disbursement(...)
```

The NNS `initiate_maturity_disbursement()` also explicitly checks for the spawning state:

```rust
// rs/nns/governance/src/governance/disburse_maturity.rs ~line 300
if is_neuron_spawning {
    return Err(InitiateMaturityDisbursementError::NeuronSpawning);
}
```

The SNS `disburse_maturity()` performs neither check. It directly reads and mutates the neuron without verifying `in_flight_commands`:

```rust
// rs/sns/governance/src/governance.rs ~line 1609-1706
pub fn disburse_maturity(...) -> Result<DisburseMaturityResponse, GovernanceError> {
    let neuron = self.get_neuron_result(id)?;
    neuron.check_authorized(caller, NeuronPermissionType::DisburseMaturity)?;
    // ... percentage and fee checks only ...
    let neuron = self.get_neuron_result_mut(id)?;
    neuron.maturity_e8s_equivalent = neuron.maturity_e8s_equivalent.saturating_sub(maturity_to_deduct);
    neuron.disburse_maturity_in_progress.push(disbursement_in_progress);
    // No lock check anywhere
}
```

By contrast, SNS `disburse_neuron` (async) and the SNS `finalize_disburse_maturity` timer both call `lock_neuron_for_command` before touching neuron state.

The `in_flight_commands` map is the sole mechanism preventing concurrent state mutation during async ledger calls. Its purpose is documented explicitly:

> "An entry being present in this map acts like a 'lock' on the neuron and thus prevents concurrent changes that might happen due to the interleaving of user requests and callback execution."

### Impact Explanation

Because IC canisters process messages sequentially but yield control at `await` points, the following interleaving is reachable:

1. Caller invokes `disburse_neuron` (async) — neuron lock is acquired, a ledger transfer call is issued, execution yields.
2. While the canister awaits the ledger response, a second ingress message `disburse_maturity` arrives and is processed synchronously.
3. `disburse_maturity` bypasses the lock, deducts `maturity_e8s_equivalent`, and appends to `disburse_maturity_in_progress`.
4. `disburse_neuron` resumes. If it fails and retains the lock (via `lock.retain()`), the neuron remains permanently locked.
5. The SNS `finalize_disburse_maturity` timer skips locked neurons (`Err(_) => continue`), so the scheduled maturity disbursement can never be finalized.
6. The maturity has been deducted from the neuron but will never be transferred — it is permanently lost.

Even in the non-failure case, the lock invariant is violated: neuron state is mutated while an async ledger operation is in progress, which the lock system is specifically designed to prevent.

### Likelihood Explanation

The entry path is a standard unprivileged ingress `manage_neuron` call with `DisburseMaturity` permission, which any neuron controller or hot-key holder possesses. No special privileges, admin keys, or threshold corruption are required. The timing window exists whenever any async neuron operation (e.g., `disburse_neuron`, `split_neuron`) is awaiting a ledger response. On a loaded canister this window can span multiple rounds.

### Recommendation

Add a neuron lock acquisition to SNS `disburse_maturity()`, mirroring the NNS implementation:

```rust
pub fn disburse_maturity(...) -> Result<DisburseMaturityResponse, GovernanceError> {
    let now_seconds = self.env.now();
    let in_flight_command = NeuronInFlightCommand {
        timestamp: now_seconds,
        command: Some(neuron_in_flight_command::Command::SyncCommand(SyncCommand {})),
    };
    let _neuron_lock = self.lock_neuron_for_command(id, in_flight_command)?;
    // ... rest of function unchanged ...
}
```

### Proof of Concept

**Vulnerable function — no lock check:** [1](#0-0) 

**NNS counterpart — acquires lock before any mutation:** [2](#0-1) 

**NNS `initiate_maturity_disbursement` — also checks spawning state:** [3](#0-2) 

**SNS `finalize_disburse_maturity` timer — skips locked neurons, leaving deducted maturity stranded:** [4](#0-3) 

**SNS `lock_neuron_for_command` — the guard that `disburse_maturity` omits:** [5](#0-4) 

**Lock purpose documented in proto:** [6](#0-5)

### Citations

**File:** rs/sns/governance/src/governance.rs (L904-920)
```rust
    fn lock_neuron_for_command(
        &mut self,
        nid: &NeuronId,
        command: NeuronInFlightCommand,
    ) -> Result<LedgerUpdateLock, GovernanceError> {
        let nid = nid.to_string();
        if self.proto.in_flight_commands.contains_key(&nid) {
            return Err(GovernanceError::new_with_message(
                ErrorType::NeuronLocked,
                "Neuron has an ongoing operation.",
            ));
        }

        self.proto.in_flight_commands.insert(nid.clone(), command);

        Ok(LedgerUpdateLock { nid, gov: self })
    }
```

**File:** rs/sns/governance/src/governance.rs (L1609-1616)
```rust
    pub fn disburse_maturity(
        &mut self,
        id: &NeuronId,
        caller: &PrincipalId,
        disburse_maturity: &DisburseMaturity,
    ) -> Result<DisburseMaturityResponse, GovernanceError> {
        let neuron = self.get_neuron_result(id)?;
        neuron.check_authorized(caller, NeuronPermissionType::DisburseMaturity)?;
```

**File:** rs/sns/governance/src/governance.rs (L5006-5009)
```rust
            let _neuron_lock = match self.lock_neuron_for_command(&neuron_id, in_flight_command) {
                Ok(neuron_lock) => neuron_lock,
                Err(_) => continue, // if locking fails, try next neuron
            };
```

**File:** rs/nns/governance/src/governance.rs (L3091-3119)
```rust
    #[cfg_attr(feature = "tla", tla_update_method(DISBURSE_MATURITY_DESC.clone(), tla_snapshotter!()))]
    fn disburse_maturity(
        &mut self,
        id: &NeuronId,
        caller: &PrincipalId,
        disburse_maturity: &manage_neuron::DisburseMaturity,
    ) -> Result<u64, GovernanceError> {
        self.check_heap_can_grow()?;

        let now_seconds = self.env.now();

        let in_flight_command = NeuronInFlightCommand {
            timestamp: now_seconds,
            command: Some(InFlightCommand::SyncCommand(SyncCommand {})),
        };

        // Lock the neuron so that we're sure that we are not disbursing the maturity in the middle
        // of another ongoing operation.
        let _neuron_lock = self.lock_neuron_for_command(id.id, in_flight_command)?;

        initiate_maturity_disbursement(
            &mut self.neuron_store,
            caller,
            id,
            disburse_maturity,
            now_seconds,
        )
        .map_err(GovernanceError::from)
    }
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L300-302)
```rust
    if is_neuron_spawning {
        return Err(InitiateMaturityDisbursementError::NeuronSpawning);
    }
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L1471-1492)
```text
  // The in-flight neuron ledger commands as a map from neuron IDs
  // to commands.
  //
  // Whenever we change a neuron in a way that must not interleave
  // with another neuron change, we store the neuron and the issued
  // command in this map and remove it when the command is complete.
  //
  // An entry being present in this map acts like a "lock" on the neuron
  // and thus prevents concurrent changes that might happen due to the
  // interleaving of user requests and callback execution.
  //
  // If there are no ongoing requests, this map should be empty.
  //
  // If something goes fundamentally wrong (say we trap at some point
  // after issuing a transfer call) the neuron(s) involved are left in a
  // "locked" state, meaning new operations can't be applied without
  // reconciling the state.
  //
  // Because we know exactly what was going on, we should have the
  // information necessary to reconcile the state, using custom code
  // added on upgrade, if necessary.
  map<string, NeuronInFlightCommand> in_flight_commands = 10;
```
