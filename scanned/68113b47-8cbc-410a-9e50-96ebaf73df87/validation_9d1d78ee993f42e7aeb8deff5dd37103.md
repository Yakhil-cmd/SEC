### Title
Unbounded Iteration Over All Neurons in `maybe_finalize_disburse_maturity` Can Exhaust Instruction Limit - (File: rs/sns/governance/src/governance.rs)

### Summary
The SNS governance canister's `maybe_finalize_disburse_maturity` function iterates over the entire neuron map without any bound, collecting all neurons with pending maturity disbursements and then processing every one of them — including async cross-canister ledger calls — in a single periodic task execution. As the number of SNS neurons grows, or as users accumulate many pending disbursements, this unbounded scan can exhaust the IC instruction limit, causing the periodic task to trap and leaving maturity disbursements permanently stuck.

### Finding Description

`maybe_finalize_disburse_maturity` in `rs/sns/governance/src/governance.rs` first scans every neuron in `self.proto.neurons.values()` to collect all neurons whose first pending disbursement has passed its finalization timestamp: [1](#0-0) 

It then iterates over the entire collected set, performing an `await`-ed cross-canister ledger transfer for each entry: [2](#0-1) 

There is no cap on the number of neurons scanned or disbursements processed per invocation. The IC enforces a per-message instruction limit (currently ~20B instructions for update calls / DTS slices). A sufficiently large neuron map — or a burst of disbursements all becoming finalizable at the same time — will cause the scan or the processing loop to exceed this limit, trapping the periodic task.

The same file also contains `maybe_move_staked_maturity`, which iterates over all neurons without any bound: [3](#0-2) 

**Contrast with the NNS governance fix:** The NNS governance canister already addressed this exact pattern. Its `finalize_maturity_disbursement` processes exactly **one** disbursement per timer invocation and uses an indexed lookup (`get_neuron_ids_ready_to_finalize_maturity_disbursement`) instead of a full scan: [4](#0-3) 

The NNS `unstake_maturity_of_dissolved_neurons` similarly caps work per call at `MAX_NEURONS_TO_UNSTAKE = 100`: [5](#0-4) 

The SNS governance has no equivalent safeguard.

### Impact Explanation
If `maybe_finalize_disburse_maturity` traps due to instruction exhaustion, `is_finalizing_disburse_maturity` is never reset to `None` (line 5082 is never reached), permanently blocking all future finalization attempts via `can_finalize_disburse_maturity`. Users' maturity disbursements become permanently stuck in the canister — a direct ledger conservation bug. Even if the flag is eventually cleared by an upgrade, the next run will trap again under the same load. [6](#0-5) 

### Likelihood Explanation
Any SNS with a large and active user base (thousands of neurons) is at risk. The `disburse_maturity` operation is a standard, unprivileged user action. A user with a neuron can call it repeatedly (subject only to the per-neuron `MAX_NUM_DISBURSEMENTS` limit in NNS, but no such limit is enforced in SNS governance for the aggregate across all neurons). A coordinated or organic burst of disbursements all maturing simultaneously is realistic on a popular SNS. [7](#0-6) 

### Recommendation
1. Replace the full-scan + process-all loop in `maybe_finalize_disburse_maturity` with a bounded approach: process at most N disbursements per periodic task invocation (analogous to `MAX_NEURONS_TO_UNSTAKE = 100` in NNS governance).
2. Introduce a maturity disbursement index in SNS governance (as NNS governance does via `get_neuron_ids_ready_to_finalize_maturity_disbursement`) to avoid scanning all neurons on every tick.
3. Apply the same fix to `maybe_move_staked_maturity`, which also iterates all neurons without a bound.
4. Ensure `is_finalizing_disburse_maturity` is always reset (e.g., via a guard/RAII pattern) even if the function traps mid-execution.

### Proof of Concept

1. Deploy an SNS with a large number of neurons (e.g., 10,000+).
2. Have many neuron controllers call `disburse_maturity` so that thousands of `DisburseMaturityInProgress` entries accumulate with the same `finalize_disbursement_timestamp_seconds`.
3. Wait for the finalization timestamp to pass.
4. Observe that the next invocation of `run_periodic_tasks` → `maybe_finalize_disburse_maturity` scans all neurons and attempts to process all ready disbursements in one message execution.
5. The instruction counter exceeds the IC limit; the message traps.
6. `is_finalizing_disburse_maturity` remains `Some(true)`, blocking all subsequent finalization attempts.
7. All pending maturity disbursements are permanently stuck. [8](#0-7)

### Citations

**File:** rs/sns/governance/src/governance.rs (L4920-4935)
```rust
    // Disburses any maturity that should be disbursed, unless this is already happening.
    async fn maybe_finalize_disburse_maturity(&mut self) {
        if !self.can_finalize_disburse_maturity() {
            return;
        }

        let maturity_modulation_basis_points =
            match self.proto.effective_maturity_modulation_basis_points() {
                Ok(maturity_modulation_basis_points) => maturity_modulation_basis_points,
                Err(message) => {
                    log!(ERROR, "{}", message.error_message);
                    return;
                }
            };

        self.proto.is_finalizing_disburse_maturity = Some(true);
```

**File:** rs/sns/governance/src/governance.rs (L4937-5046)
```rust
        // Filter all the neurons that are ready to disburse.
        let neuron_id_and_disbursements: Vec<(NeuronId, DisburseMaturityInProgress)> = self
            .proto
            .neurons
            .values()
            .filter_map(|neuron| {
                let id = match neuron.id.as_ref() {
                    Some(id) => id,
                    None => {
                        log!(
                            ERROR,
                            "NeuronId is not set for neuron. This should never happen. \
                             Cannot disburse."
                        );
                        return None;
                    }
                };
                // The first entry is the oldest one, check whether it can be completed.
                let first_disbursement = neuron.disburse_maturity_in_progress.first()?;
                let finalize_disbursement_timestamp_seconds =
                    match first_disbursement.finalize_disbursement_timestamp_seconds {
                        Some(finalize_disbursement_timestamp_seconds) => {
                            finalize_disbursement_timestamp_seconds
                        }
                        None => {
                            log!(
                                ERROR,
                                "Finalize disbursement timestamp is not set. Cannot disburse."
                            );
                            return None;
                        }
                    };
                if now_seconds >= finalize_disbursement_timestamp_seconds {
                    Some((id.clone(), first_disbursement.clone()))
                } else {
                    None
                }
            })
            .collect();
        for (neuron_id, disbursement) in neuron_id_and_disbursements.into_iter() {
            let maturity_to_disburse_after_modulation_e8s: u64 = match apply_maturity_modulation(
                disbursement.amount_e8s,
                maturity_modulation_basis_points,
            ) {
                Ok(maturity_to_disburse_after_modulation_e8s) => {
                    maturity_to_disburse_after_modulation_e8s
                }
                Err(err) => {
                    log!(
                        ERROR,
                        "Could not apply maturity modulation to {:?} for neuron {} due to {:?}, skipping",
                        disbursement,
                        neuron_id,
                        err
                    );
                    continue;
                }
            };

            let fdm = FinalizeDisburseMaturity {
                amount_to_be_disbursed_e8s: maturity_to_disburse_after_modulation_e8s,
                to_account: disbursement.account_to_disburse_to.clone(),
            };
            let in_flight_command = NeuronInFlightCommand {
                timestamp: self.env.now(),
                command: Some(neuron_in_flight_command::Command::FinalizeDisburseMaturity(
                    fdm,
                )),
            };
            let _neuron_lock = match self.lock_neuron_for_command(&neuron_id, in_flight_command) {
                Ok(neuron_lock) => neuron_lock,
                Err(_) => continue, // if locking fails, try next neuron
            };
            // Do the transfer, this is a minting transfer, from the governance canister's
            // main account (which is also the minting account) to the provided account.
            let account_proto = match disbursement.account_to_disburse_to {
                Some(ref proto) => proto.clone(),
                None => {
                    log!(
                        ERROR,
                        "Invalid DisburseMaturityInProgress-entry {:?} for neuron {}, skipping.",
                        disbursement,
                        neuron_id
                    );
                    continue;
                }
            };
            let to_account = match Account::try_from(account_proto) {
                Ok(account) => account,
                Err(e) => {
                    log!(
                        ERROR,
                        "Failure parsing account of DisburseMaturityInProgress-entry {:?} for neuron {}: {}.",
                        disbursement,
                        neuron_id,
                        e
                    );
                    continue;
                }
            };
            let transfer_result = self
                .ledger
                .transfer_funds(
                    maturity_to_disburse_after_modulation_e8s,
                    0,    // Minting transfers don't pay a fee.
                    None, // This is a minting transfer, no 'from' account is needed
                    to_account,
                    self.env.now(), // The memo(nonce) for the ledger's transaction
                )
                .await;
```

**File:** rs/sns/governance/src/governance.rs (L5082-5083)
```rust
        self.proto.is_finalizing_disburse_maturity = None;
    }
```

**File:** rs/sns/governance/src/governance.rs (L5087-5099)
```rust
    pub(crate) fn maybe_move_staked_maturity(&mut self) {
        let now_seconds = self.env.now();
        // Filter all the neurons that are currently in "dissolved" state and have some staked maturity.
        for neuron in self.proto.neurons.values_mut().filter(|n| {
            n.state(now_seconds) == NeuronState::Dissolved
                && n.staked_maturity_e8s_equivalent.unwrap_or(0) > 0
        }) {
            neuron.maturity_e8s_equivalent = neuron
                .maturity_e8s_equivalent
                .saturating_add(neuron.staked_maturity_e8s_equivalent.unwrap_or(0));
            neuron.staked_maturity_e8s_equivalent = None;
        }
    }
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L451-470)
```rust
fn next_maturity_disbursement_to_finalize(
    neuron_store: &NeuronStore,
    in_flight_commands: &HashMap<u64, NeuronInFlightCommand>,
    maturity_modulation_basis_points: Option<i32>,
    now_seconds: u64,
) -> Result<Option<MaturityDisbursementFinalization>, FinalizeMaturityDisbursementError> {
    let maturity_modulation_basis_points = maturity_modulation_basis_points
        .ok_or(FinalizeMaturityDisbursementError::NoMaturityModulation)?;

    // Try to find the first neuron eligible for finalizing maturity disbursement, that is not
    // locked.
    let Some(neuron_id) = neuron_store
        .get_neuron_ids_ready_to_finalize_maturity_disbursement(now_seconds)
        .into_iter()
        .find(|neuron_id| !in_flight_commands.contains_key(&neuron_id.id))
    else {
        // If all neurons are locked, we don't need to finalize anything.
        return Ok(None);
    };
    // Either of the errors below indicates a bug in the maturity disbursement index.
```

**File:** rs/nns/governance/src/governance.rs (L6388-6397)
```rust
    pub fn unstake_maturity_of_dissolved_neurons(&mut self) {
        // We assume that modifying a neuron can use <400 StableBTreeMap read operations and <400
        // write operations (100 recent ballots + 270 followees entries + others), and one read + one
        // write operation takes 400K instructions in total, unstaking 100 neurons should take less
        // than 16B instructions. Note that this is the worst case scenario, and the actual number
        // of instructions should be much less.
        const MAX_NEURONS_TO_UNSTAKE: usize = 100;
        let now_seconds = self.env.now();
        self.neuron_store
            .unstake_maturity_of_dissolved_neurons(now_seconds, MAX_NEURONS_TO_UNSTAKE);
```
