### Title
Unbounded Linear Iteration with Sequential Ledger Calls in SNS Governance Periodic Task Enables Governance Liveness Degradation — (`rs/sns/governance/src/governance.rs`)

---

### Summary

The SNS governance canister's `maybe_finalize_disburse_maturity` function collects **all** neurons with matured disbursements into an unbounded `Vec` and then iterates over every entry, issuing a sequential cross-canister `transfer_funds` call (`.await`) for each one inside a single heartbeat invocation. An unprivileged attacker who controls many SNS neurons can force this loop to make an arbitrarily large number of sequential ledger round-trips in one heartbeat, occupying the governance canister for many consensus rounds and degrading governance liveness. The NNS governance canister has already fixed this exact pattern by processing exactly one disbursement per timer invocation; the SNS canister retains the old unbounded design.

---

### Finding Description

`maybe_finalize_disburse_maturity` in `rs/sns/governance/src/governance.rs` first collects every neuron whose first pending disbursement has passed its `finalize_disbursement_timestamp_seconds`:

```rust
// rs/sns/governance/src/governance.rs  ~L4938-4975
let neuron_id_and_disbursements: Vec<(NeuronId, DisburseMaturityInProgress)> = self
    .proto
    .neurons
    .values()
    .filter_map(|neuron| {
        ...
        if now_seconds >= finalize_disbursement_timestamp_seconds {
            Some((id.clone(), first_disbursement.clone()))
        } else {
            None
        }
    })
    .collect();
```

It then iterates over the entire collected set and issues a blocking cross-canister ledger call for each entry:

```rust
// rs/sns/governance/src/governance.rs  ~L4976-5046
for (neuron_id, disbursement) in neuron_id_and_disbursements.into_iter() {
    ...
    let transfer_result = self
        .ledger
        .transfer_funds(
            maturity_to_disburse_after_modulation_e8s,
            0,
            None,
            to_account,
            self.env.now(),
        )
        .await;          // ← sequential cross-canister call inside unbounded loop
    ...
}
``` [1](#0-0) [2](#0-1) 

Each `.await` suspends the heartbeat and waits for a full ledger round-trip before the next iteration begins. There is no cap on the number of neurons processed per invocation. The only guard is the `is_finalizing_disburse_maturity` flag, which prevents re-entry but does nothing to bound the work done within a single invocation. [3](#0-2) 

This function is called unconditionally from `run_periodic_tasks`, the SNS governance heartbeat: [4](#0-3) 

**Contrast with the fixed NNS design.** The NNS governance canister has already addressed this exact pattern. Its `try_finalize_maturity_disbursement` processes exactly **one** disbursement per timer invocation by finding only the first eligible neuron: [5](#0-4) 

The NNS timer task then reschedules itself at the appropriate delay for the next disbursement: [6](#0-5) 

The SNS canister has no equivalent bound.

---

### Impact Explanation

An attacker who controls N SNS neurons, each with a matured disbursement, causes `maybe_finalize_disburse_maturity` to issue N sequential cross-canister calls to the SNS ledger in a single heartbeat invocation. Each call requires at least one consensus round-trip. During this period:

1. **Governance liveness degradation**: The `is_finalizing_disburse_maturity` flag is held for the entire duration, blocking any further disbursement finalization. Governance proposals that depend on timely maturity disbursement are delayed proportionally to N.
2. **Cycles drain**: The governance canister pays cycles for every ledger call. With a large N, this constitutes an attacker-directed cycles drain on the governance canister funded by the SNS treasury.
3. **Heartbeat starvation**: Other periodic tasks scheduled within `run_periodic_tasks` (reward distribution, upgrade checks, etc.) are delayed because the heartbeat is occupied awaiting sequential ledger responses.

This is the IC analog of the reported "unmetered EndBlock gas" pattern: work paid for once (initiating disbursements) generates unbounded subsidized work in the periodic task.

---

### Likelihood Explanation

The attack is permissionless for any party holding sufficient SNS tokens to create many neurons. The steps are:

1. Acquire SNS tokens and create N neurons (standard SNS user flow).
2. Call `DisburseMaturity` on each neuron to schedule disbursements (standard governance operation, no privileged access required).
3. Wait 7 days for the disbursement delay to elapse.
4. At the next heartbeat after maturity, `maybe_finalize_disburse_maturity` processes all N disbursements sequentially.

The incubation period (7 days) matches the `MATURITY_DISBURSEMENT_DELAY_SECONDS` constant and blends with normal user behavior, making the attack difficult to detect in advance. The cost to the attacker is the opportunity cost of locking SNS tokens in neurons, which is recoverable after dissolving.

---

### Recommendation

Apply the same fix already used in the NNS governance canister: process exactly one maturity disbursement per timer/heartbeat invocation and reschedule the task for the next eligible disbursement. Replace the unbounded `for` loop in `maybe_finalize_disburse_maturity` with a single-disbursement pattern analogous to `try_finalize_maturity_disbursement` in `rs/nns/governance/src/governance/disburse_maturity.rs`, and convert the SNS periodic task to a `RecurringAsyncTask` that self-schedules based on the next pending disbursement timestamp. [7](#0-6) 

---

### Proof of Concept

1. Deploy an SNS instance on a test network.
2. Create N neurons (e.g., N = 1000) each holding the minimum stake, all controlled by the attacker.
3. Call `ManageNeuron { DisburseMaturity { percentage_to_disburse: 100, ... } }` on each neuron.
4. Advance the test clock by 7 days + 1 second so all disbursements mature simultaneously.
5. Trigger a heartbeat. Observe that `maybe_finalize_disburse_maturity` issues N sequential `transfer_funds` calls to the SNS ledger, each requiring a round-trip, occupying the governance canister for N rounds.
6. Measure the delay in processing other governance operations (e.g., proposal execution) during this period. The delay scales linearly with N.

The relevant loop is at: [8](#0-7)

### Citations

**File:** rs/sns/governance/src/governance.rs (L4920-5046)
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
        let now_seconds = self.env.now();
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

**File:** rs/sns/governance/src/governance.rs (L5527-5528)
```rust
        self.maybe_finalize_disburse_maturity().await;

```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L462-469)
```rust
    let Some(neuron_id) = neuron_store
        .get_neuron_ids_ready_to_finalize_maturity_disbursement(now_seconds)
        .into_iter()
        .find(|neuron_id| !in_flight_commands.contains_key(&neuron_id.id))
    else {
        // If all neurons are locked, we don't need to finalize anything.
        return Ok(None);
    };
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L544-554)
```rust
pub async fn finalize_maturity_disbursement(
    governance: &'static LocalKey<RefCell<Governance>>,
) -> Duration {
    match try_finalize_maturity_disbursement(governance).await {
        Ok(_) => governance.with_borrow(get_delay_until_next_finalization),
        Err(err) => {
            println!("FinalizeMaturityDisbursementTask failed: {}", err);
            RETRY_INTERVAL
        }
    }
}
```

**File:** rs/nns/governance/src/timer_tasks/finalize_maturity_disbursements.rs (L21-33)
```rust
impl RecurringAsyncTask for FinalizeMaturityDisbursementsTask {
    async fn execute(self) -> (Duration, Self) {
        let delay = finalize_maturity_disbursement(self.governance).await;
        (delay, self)
    }

    fn initial_delay(&self) -> Duration {
        self.governance
            .with_borrow(get_delay_until_next_finalization)
    }

    const NAME: &'static str = "finalize_maturity_disbursements";
}
```
