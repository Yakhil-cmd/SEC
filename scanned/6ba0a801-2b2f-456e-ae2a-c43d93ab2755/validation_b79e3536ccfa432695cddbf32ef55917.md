### Title
Unbounded Loop Over All Pending Maturity Disbursements in SNS Governance Periodic Task - (File: rs/sns/governance/src/governance.rs)

### Summary

The SNS governance canister's `maybe_finalize_disburse_maturity` function, called from `run_periodic_tasks`, collects and processes **all** neurons with ready-to-finalize maturity disbursements in a single unbounded loop, making one sequential async ledger transfer call per neuron. An attacker who controls many SNS neurons can queue many disbursements simultaneously, causing the periodic task to make an unbounded number of sequential cross-canister calls, starving other governance operations.

### Finding Description

`maybe_finalize_disburse_maturity` in `rs/sns/governance/src/governance.rs` (lines 4920–5082) first collects all ready disbursements across all neurons into an unbounded `Vec`:

```rust
let neuron_id_and_disbursements: Vec<(NeuronId, DisburseMaturityInProgress)> = self
    .proto
    .neurons
    .values()
    .filter_map(|neuron| { ... })
    .collect();
```

It then iterates over every entry, making a sequential async ledger call for each:

```rust
for (neuron_id, disbursement) in neuron_id_and_disbursements.into_iter() {
    // ...
    let transfer_result = self.ledger.transfer_funds(...).await;
    // ...
}
```

There is no batch size cap. The function is called from `run_periodic_tasks`, which is scheduled on a timer:

```rust
pub async fn run_periodic_tasks(&mut self) {
    self.process_proposals();
    // ...
    self.maybe_finalize_disburse_maturity().await;
    self.maybe_move_staked_maturity();
    self.compute_cached_metrics().await;
    self.maybe_gc();
}
```

Because `run_periodic_tasks` awaits the entire loop before returning, a large queue of disbursements causes the single async execution to span many consensus rounds (one round-trip to the ledger per disbursement). During this time the `is_finalizing_disburse_maturity` guard is held, and subsequent timer firings of `run_periodic_tasks` skip `maybe_finalize_disburse_maturity` but the overall periodic task pipeline is delayed.

By contrast, the NNS governance uses a dedicated `FinalizeMaturityDisbursementsTask` that processes exactly **one** disbursement per invocation and reschedules itself, preventing any single execution from monopolizing the canister.

The SNS `disburse_maturity` function (lines 1609–1706) only enforces that the disbursement amount exceeds the transaction fee after worst-case maturity modulation. There is no cap on the total number of neurons that may have pending disbursements across the SNS. An attacker who stakes the minimum required tokens across many neurons can queue an arbitrarily large number of disbursements.

### Impact Explanation

When the attacker's disbursements all mature simultaneously (after the 7-day `MATURITY_DISBURSEMENT_DELAY_SECONDS`), `maybe_finalize_disburse_maturity` attempts to process all of them in one invocation. Each ledger call takes at least one consensus round. With N disbursements, `run_periodic_tasks` occupies the canister for at least N rounds. This:

1. Delays `process_proposals`, preventing timely execution or rejection of SNS proposals.
2. Delays reward distribution (`distribute_rewards`).
3. Delays `maybe_move_staked_maturity` and `compute_cached_metrics`.
4. Holds the `is_finalizing_disburse_maturity` lock, blocking any concurrent finalization.

If the instruction budget for the synchronous collection phase (iterating over all neurons) is exceeded, the canister traps and rolls back the entire `run_periodic_tasks` call, meaning `process_proposals` and all subsequent tasks also fail for that round.

### Likelihood Explanation

Any SNS token holder can create neurons by staking the minimum required amount. The minimum stake is SNS-configurable and can be small. An attacker with sufficient tokens (or colluding with other token holders) can create many neurons, call `disburse_maturity` on each, and wait for the 7-day delay. This is a fully unprivileged, on-chain action requiring no special access.

### Recommendation

Apply a per-invocation batch limit to `maybe_finalize_disburse_maturity`, analogous to the NNS approach: process at most one (or a small fixed number of) disbursement(s) per timer invocation and reschedule. The NNS `FinalizeMaturityDisbursementsTask` / `finalize_maturity_disbursement` pattern (processing one disbursement per call with a retry interval) is the correct model to follow.

### Proof of Concept

1. Attacker stakes the minimum SNS token amount across N neurons (e.g., N = 500).
2. Attacker calls `manage_neuron` → `DisburseMaturity` on each neuron, queuing one disbursement per neuron.
3. After `MATURITY_DISBURSEMENT_DELAY_SECONDS` (7 days), all N disbursements become eligible.
4. The next `run_periodic_tasks` timer fires and calls `maybe_finalize_disburse_maturity`.
5. The function collects all N entries into `neuron_id_and_disbursements` (unbounded), then loops, making N sequential `ledger.transfer_funds(...).await` calls.
6. Each call requires at least one consensus round; the function holds the canister for ≥ N rounds.
7. `process_proposals` and all subsequent periodic tasks are blocked for the duration.

**Key code references:**

Unbounded collection of all ready disbursements: [1](#0-0) 

Unbounded loop with sequential async ledger calls: [2](#0-1) 

`run_periodic_tasks` awaiting the entire loop before continuing: [3](#0-2) 

No batch limit in the SNS `disburse_maturity` entry point (only a minimum-amount check): [4](#0-3) 

Contrast — NNS processes exactly one disbursement per timer invocation: [5](#0-4)

### Citations

**File:** rs/sns/governance/src/governance.rs (L1669-1698)
```rust
        if worst_case_maturity_modulation < transaction_fee_e8s {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!(
                    "If worst case maturity modulation is applied (-5%) then this neuron would \
                     disburse {worst_case_maturity_modulation} e8s, but can't disburse an amount less than the transaction fee \
                     of {transaction_fee_e8s} e8s."
                ),
            ));
        }

        let now_seconds = self.env.now();
        let disbursement_in_progress = DisburseMaturityInProgress {
            amount_e8s: maturity_to_deduct,
            timestamp_of_disbursement_seconds: now_seconds,
            account_to_disburse_to: Some(to_account_proto),
            finalize_disbursement_timestamp_seconds: Some(
                now_seconds + MATURITY_DISBURSEMENT_DELAY_SECONDS,
            ),
        };

        // Re-borrow the neuron mutably to update now that the maturity has been
        // deducted and is waiting until the end of the window to modulate and disburse.
        let neuron = self.get_neuron_result_mut(id)?;
        neuron.maturity_e8s_equivalent = neuron
            .maturity_e8s_equivalent
            .saturating_sub(maturity_to_deduct);
        neuron
            .disburse_maturity_in_progress
            .push(disbursement_in_progress);
```

**File:** rs/sns/governance/src/governance.rs (L4938-4975)
```rust
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
```

**File:** rs/sns/governance/src/governance.rs (L4976-5081)
```rust
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
            match transfer_result {
                Ok(block_index) => {
                    log!(
                        INFO,
                        "Transferring DisburseMaturityInProgress-entry {:?} for neuron {} at block {}.",
                        disbursement,
                        neuron_id,
                        block_index
                    );
                    let neuron = match self.get_neuron_result_mut(&neuron_id) {
                        Ok(neuron) => neuron,
                        Err(e) => {
                            log!(
                                ERROR,
                                "Failed updating DisburseMaturityInProgress-entry {:?} for neuron {}: {}.",
                                disbursement,
                                neuron_id,
                                e
                            );
                            continue;
                        }
                    };
                    neuron.disburse_maturity_in_progress.remove(0);
                }
                Err(e) => {
                    log!(
                        ERROR,
                        "Failed transferring funds for DisburseMaturityInProgress-entry {:?} for neuron {}: {}.",
                        disbursement,
                        neuron_id,
                        e
                    );
                }
            }
        }
```

**File:** rs/sns/governance/src/governance.rs (L5471-5534)
```rust
    /// Runs periodic tasks that are not directly triggered by user input.
    pub async fn run_periodic_tasks(&mut self) {
        use ic_cdk::println;

        self.process_proposals();

        // None of the upgrade-related tasks should interleave with one another or themselves, so we acquire a global
        // lock for the duration of their execution. This will return `false` if the lock has already been acquired less
        // than 10 minutes ago by a previous invocation of `run_periodic_tasks`, in which case we skip the
        // upgrade-related tasks.
        if self.acquire_upgrade_periodic_task_lock() {
            // We only want to check the upgrade status if we are currently executing an upgrade.
            if self.should_check_upgrade_status() {
                self.check_upgrade_status().await;
            }

            if self.should_refresh_cached_upgrade_steps() {
                match self.try_temporarily_lock_refresh_cached_upgrade_steps() {
                    Err(err) => {
                        log!(ERROR, "{}", err);
                    }
                    Ok(deployed_version) => {
                        self.refresh_cached_upgrade_steps(deployed_version).await;
                    }
                }
            }

            self.initiate_upgrade_if_sns_behind_target_version().await;

            self.release_upgrade_periodic_task_lock();
        }

        let should_distribute_rewards = self.should_distribute_rewards();

        // Getting the total governance token supply from the ledger is expensive enough
        // that we don't want to do it on every call to `run_periodic_tasks`. So
        // we only fetch it when it's needed, which is when rewards should be
        // distributed
        if should_distribute_rewards {
            match self.ledger.total_supply().await {
                Ok(supply) => {
                    // Distribute rewards
                    self.distribute_rewards(supply);
                }
                Err(e) => log!(
                    ERROR,
                    "Error when getting total governance token supply: {}",
                    GovernanceError::from(e)
                ),
            }
        }

        if self.should_update_maturity_modulation() {
            self.update_maturity_modulation().await;
        }

        self.maybe_finalize_disburse_maturity().await;

        self.maybe_move_staked_maturity();

        self.compute_cached_metrics().await;

        self.maybe_gc();
    }
```

**File:** rs/nns/governance/src/timer_tasks/finalize_maturity_disbursements.rs (L20-25)
```rust
#[async_trait]
impl RecurringAsyncTask for FinalizeMaturityDisbursementsTask {
    async fn execute(self) -> (Duration, Self) {
        let delay = finalize_maturity_disbursement(self.governance).await;
        (delay, self)
    }
```
