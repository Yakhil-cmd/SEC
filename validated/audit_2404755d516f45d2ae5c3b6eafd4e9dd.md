### Title
Unbounded Loop with Sequential Inter-Canister Calls in `maybe_finalize_disburse_maturity` Can Permanently Freeze SNS Maturity Disbursements - (File: `rs/sns/governance/src/governance.rs`)

---

### Summary

The SNS governance canister's `maybe_finalize_disburse_maturity` function collects **all** neurons with pending maturity disbursements and processes them in a single unbounded `for` loop, issuing a sequential `await`-ed inter-canister call to the ledger for each neuron. There is no instruction-limit guard between iterations. If the loop exhausts the per-message instruction budget mid-execution, the `is_finalizing_disburse_maturity` guard flag—committed to stable state before the first `await`—remains permanently set to `Some(true)`, causing every subsequent invocation of `maybe_finalize_disburse_maturity` to return immediately. This permanently freezes all SNS maturity disbursements.

---

### Finding Description

`maybe_finalize_disburse_maturity` is called from `run_periodic_tasks`, which is driven by a repeating timer:

```
rs/sns/governance/canister/canister.rs  line 632
ic_cdk_timers::set_timer_interval(RUN_PERIODIC_TASKS_INTERVAL, async || {
    run_periodic_tasks().await
});
```

Inside `run_periodic_tasks`, the call chain is:

```
rs/sns/governance/src/governance.rs  line 5527
self.maybe_finalize_disburse_maturity().await;
```

`maybe_finalize_disburse_maturity` first sets the guard flag and then iterates over every eligible neuron, issuing one ledger call per neuron:

```rust
// line 4935 – committed to state at the first await below
self.proto.is_finalizing_disburse_maturity = Some(true);

// lines 4938-4975 – collect ALL neurons ready to disburse
let neuron_id_and_disbursements: Vec<...> = self.proto.neurons.values()
    .filter_map(|neuron| { ... })
    .collect();

// lines 4976-5081 – unbounded loop, one ledger await per neuron, no instruction check
for (neuron_id, disbursement) in neuron_id_and_disbursements.into_iter() {
    ...
    let transfer_result = self.ledger.transfer_funds(...).await;  // ← inter-canister call
    ...
}

// line 5082 – only reached if the loop completes without trapping
self.proto.is_finalizing_disburse_maturity = None;
``` [1](#0-0) [2](#0-1) [3](#0-2) 

In the IC execution model, every `await` is a message boundary: state mutations that occurred before the `await` are committed to the replicated state when the outgoing call is enqueued. Therefore:

1. `is_finalizing_disburse_maturity = Some(true)` is written.
2. The first `await` (first ledger call) commits that write.
3. If the canister traps on any subsequent iteration—because the instruction budget is exhausted—the IC rolls back only the current execution slice; the committed flag value `Some(true)` survives.
4. `can_finalize_disburse_maturity()` (checked at line 4922) reads this flag and returns `false`, so every future timer invocation skips the function entirely. [4](#0-3) 

The guard flag is documented in the SNS governance proto:

```
/// True if the run_periodic_tasks function is currently finalizing disburse maturity,
/// meaning that it should finish before being called again.
pub is_finalizing_disburse_maturity: Option<bool>,
``` [5](#0-4) 

The NNS governance canister solved the same problem differently: it processes **one** disbursement per timer tick and uses a dedicated `FinalizeMaturityDisbursementsTask` that reschedules itself, so no single message can exhaust the budget. [6](#0-5) [7](#0-6) 

---

### Impact Explanation

If the guard flag is stuck at `Some(true)`, `maybe_finalize_disburse_maturity` becomes a permanent no-op. All SNS neurons whose maturity disbursements have passed the 7-day waiting period will never receive their tokens. The only recovery path is a governance-approved canister upgrade that clears the flag—requiring the SNS DAO to notice the freeze, draft a proposal, reach quorum, and execute it. Until then, every neuron holder who called `DisburseMaturity` is permanently locked out of their funds.

---

### Likelihood Explanation

An attacker who holds (or can acquire) SNS tokens can:

1. Create a large number of SNS neurons (each above the minimum stake).
2. Call `DisburseMaturity` on each neuron.
3. Wait the 7-day disbursement delay.
4. At that point, all disbursements become eligible simultaneously, and the next `run_periodic_tasks` timer tick triggers the unbounded loop.

The cost scales with the number of neurons needed to exhaust the instruction budget. Because each ledger `transfer_funds` call consumes a non-trivial number of instructions, and the per-message limit on application subnets is 5 billion instructions, a few thousand neurons may suffice. SNS token distribution is public, so an attacker can assess feasibility before committing capital. The attack is therefore realistic for a well-funded adversary targeting a high-value SNS. [8](#0-7) 

---

### Recommendation

**Short term:** Mirror the NNS governance pattern: process exactly one pending disbursement per timer invocation and reschedule immediately if more remain. Remove the `is_finalizing_disburse_maturity` boolean guard entirely, since the single-item-per-tick design makes it unnecessary.

**Long term:** Add an instruction-limit check inside the loop (analogous to `is_message_over_threshold` used in `distribute_pending_rewards`) so that even if the loop is retained, it breaks out safely before the budget is exhausted and resumes on the next tick. [9](#0-8) 

---

### Proof of Concept

1. Deploy an SNS with default parameters.
2. Acquire enough SNS tokens to create N neurons (N chosen so that N sequential ledger calls exceed the 5 B instruction limit).
3. For each neuron, call `manage_neuron` with `Command::DisburseMaturity { percentage_to_disburse: 100, to_account: ... }`.
4. Advance time by ≥ 7 days (the `MATURITY_DISBURSEMENT_DELAY_SECONDS`).
5. Observe the next `run_periodic_tasks` timer tick: the loop attempts N ledger calls, traps on instruction exhaustion, and commits `is_finalizing_disburse_maturity = Some(true)`.
6. All subsequent timer ticks call `can_finalize_disburse_maturity()`, receive `false`, and return immediately—maturity disbursements are permanently frozen without a governance upgrade. [10](#0-9) [11](#0-10)

### Citations

**File:** rs/sns/governance/src/governance.rs (L4920-4923)
```rust
    // Disburses any maturity that should be disbursed, unless this is already happening.
    async fn maybe_finalize_disburse_maturity(&mut self) {
        if !self.can_finalize_disburse_maturity() {
            return;
```

**File:** rs/sns/governance/src/governance.rs (L4935-4935)
```rust
        self.proto.is_finalizing_disburse_maturity = Some(true);
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

**File:** rs/sns/governance/src/governance.rs (L4976-5046)
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
```

**File:** rs/sns/governance/src/governance.rs (L5082-5082)
```rust
        self.proto.is_finalizing_disburse_maturity = None;
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

**File:** rs/sns/governance/src/gen/ic_sns_governance.pb.v1.rs (L2068-2071)
```rust
    /// True if the run_periodic_tasks function is currently finalizing disburse maturity, meaning
    /// that it should finish before being called again.
    #[prost(bool, optional, tag = "25")]
    pub is_finalizing_disburse_maturity: ::core::option::Option<bool>,
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

**File:** rs/nns/governance/src/timer_tasks/finalize_maturity_disbursements.rs (L20-33)
```rust
#[async_trait]
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

**File:** rs/nns/governance/src/reward/distribution.rs (L154-188)
```rust
    fn continue_processing(
        &mut self,
        neuron_store: &mut NeuronStore,
        is_over_instructions_limit: fn() -> bool,
    ) {
        while let Some((id, reward_e8s)) = self.rewards.pop_first() {
            match neuron_store.with_neuron_mut(&id, |neuron| {
                let auto_stake = neuron.auto_stake_maturity.unwrap_or(false);
                if auto_stake {
                    neuron.staked_maturity_e8s_equivalent = Some(
                        neuron
                            .staked_maturity_e8s_equivalent
                            .unwrap_or_default()
                            .saturating_add(reward_e8s),
                    );
                } else {
                    neuron.maturity_e8s_equivalent =
                        neuron.maturity_e8s_equivalent.saturating_add(reward_e8s);
                }
            }) {
                Ok(_) => {}
                Err(e) => {
                    println!(
                        "{}Error rewarding neuron {:?} during reward_distribution.\
                    This should not be possible as neuron existence is checked when \
                    rewards are calculated: {}",
                        LOG_PREFIX, id, e
                    );
                }
            };
            if is_over_instructions_limit() {
                break;
            }
        }
    }
```

**File:** rs/sns/governance/canister/canister.rs (L605-611)
```rust
async fn run_periodic_tasks() {
    if let Some(ref mut timers) = governance_mut().proto.timers {
        timers.last_spawned_timestamp_seconds.replace(now_seconds());
    };

    governance_mut().run_periodic_tasks().await;
}
```
