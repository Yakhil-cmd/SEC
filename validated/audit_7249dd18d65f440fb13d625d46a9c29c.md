### Title
Unbounded Neuron Scan in SNS Governance `maybe_finalize_disburse_maturity` Causes Instruction-Limit DoS on Periodic Disbursement Task - (File: `rs/sns/governance/src/governance.rs`)

---

### Summary

The SNS governance canister's `maybe_finalize_disburse_maturity` function performs an unbounded O(N) scan over every neuron in the canister's in-memory store on every heartbeat invocation. As the neuron population grows — either organically or through an adversary staking minimal SNS tokens to create many neurons — the pre-`await` scan segment can exhaust the IC instruction limit, permanently silencing the maturity-disbursement heartbeat path and freezing all pending disbursements. The NNS governance team has already acknowledged and patched the identical class of bug in analogous tasks (unstake-maturity, approve-genesis-KYC), but the SNS governance counterpart remains unguarded.

---

### Finding Description

`maybe_finalize_disburse_maturity` is called unconditionally from `run_periodic_tasks` on every heartbeat:

```
// rs/sns/governance/src/governance.rs  line 5527
self.maybe_finalize_disburse_maturity().await;
```

Inside the function, before any `await` suspension point, the code collects every neuron that has a ready disbursement by iterating the entire neuron map:

```rust
// rs/sns/governance/src/governance.rs  lines 4938-4975
let neuron_id_and_disbursements: Vec<(NeuronId, DisburseMaturityInProgress)> = self
    .proto
    .neurons          // ← full BTreeMap<String, Neuron>, no limit
    .values()
    .filter_map(|neuron| {
        ...
        if now_seconds >= finalize_disbursement_timestamp_seconds {
            Some((id.clone(), first_disbursement.clone()))
        } else {
            None
        }
    })
    .collect();       // ← entire result materialised before first .await
```

Because this entire scan executes in a single uninterrupted instruction-counting segment (no `await` between the start of the function and the first ledger call at line 5037), the IC runtime charges all of its instructions against the single heartbeat message slice. The IC's per-message instruction limit (40 billion instructions for update/heartbeat calls) is finite; a sufficiently large neuron population will cause the segment to be killed, the heartbeat to trap, and the disbursement task to silently stop running.

After the scan, the function iterates over every ready disbursement and issues one ledger `transfer_funds` call per neuron — also without any per-invocation cap:

```rust
// rs/sns/governance/src/governance.rs  lines 4976-5081
for (neuron_id, disbursement) in neuron_id_and_disbursements.into_iter() {
    ...
    let transfer_result = self.ledger.transfer_funds(...).await;
    ...
}
```

Each loop body between consecutive `await` points is individually bounded, but the sheer number of iterations means the heartbeat can be occupied for many consecutive rounds, starving other periodic tasks.

The NNS governance CHANGELOG explicitly records that the same class of bug was fixed there:

> *"Unstaking maturity task has a limit of 100 neurons per message, which prevents it from exceeding instruction limit."*
> *"Avoid applying `approve_genesis_kyc` to an unbounded number of neurons, but at most 1000 neurons."*

No equivalent guard exists in the SNS governance path.

---

### Impact Explanation

**Cycles/resource accounting bug → periodic-task DoS.**

Once the neuron count crosses the threshold at which the pre-`await` scan exhausts the 40 B instruction budget, every subsequent heartbeat invocation of `run_periodic_tasks` traps at the same point. All pending `disburse_maturity_in_progress` entries across every neuron are frozen indefinitely: users who initiated maturity disbursements will never receive their tokens unless the canister is upgraded with a patched implementation. Because `is_finalizing_disburse_maturity` is set to `Some(true)` before the scan and only cleared at the very end of the function, a trap mid-scan also permanently sets the guard flag, causing `can_finalize_disburse_maturity()` to return `false` on all future invocations — a secondary lock-out that compounds the DoS.

---

### Likelihood Explanation

Any principal can create SNS neurons by staking the minimum required SNS token amount. The `NervousSystemParameters.max_number_of_neurons` field caps the total, but the default ceiling for deployed SNS instances is on the order of tens of thousands to hundreds of thousands of neurons. At those scales the pre-`await` scan already consumes a non-trivial fraction of the instruction budget; with a large SNS token distribution (many small holders each creating a neuron) the threshold can be reached organically without any adversarial intent. An attacker who acquires a modest amount of SNS tokens can accelerate this by creating the maximum number of neurons, each with a pending disbursement, to maximise per-iteration cost.

---

### Recommendation

1. **Batch the scan**: process at most N neurons per heartbeat invocation (analogous to the NNS `UnstakeMaturityOfDissolvedNeuronsTask` limit of 100 neurons per message). Persist a cursor (e.g., the last-processed neuron ID) in canister state so successive heartbeats make forward progress.

2. **Clear the lock on trap**: ensure `is_finalizing_disburse_maturity` is reset to `None` even when the function panics or traps, using a RAII guard or an explicit `on_trap` hook, to prevent the secondary lock-out.

3. **Cap the disbursement loop**: even after fixing the scan, limit the number of ledger calls issued per heartbeat invocation to bound the total round-time impact.

---

### Proof of Concept

The root cause is directly visible at the call site and in the NNS CHANGELOG:

**Unbounded scan (SNS governance — unfixed):** [1](#0-0) 

**Heartbeat dispatch (no guard):** [2](#0-1) 

**NNS CHANGELOG confirming the identical class was patched in NNS governance:** [3](#0-2) 

**NNS governance equivalent task — now bounded at 100 neurons per message:** [4](#0-3)

### Citations

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

**File:** rs/sns/governance/src/governance.rs (L5527-5527)
```rust
        self.maybe_finalize_disburse_maturity().await;
```

**File:** rs/nns/governance/CHANGELOG.md (L656-668)
```markdown
          multiple messages.
        * Unstaking maturity task has a limit of 100 neurons per message, which prevents it from
          exceeding instruction limit.
        * The execution of `ApproveGenesisKyc` proposals have a limit of 1000 neurons, above which
          the proposal will fail.
        * More benchmarks were added.
* Enable timer task metrics for better observability.

## Changed

* Voting Rewards will be scheduled by a timer instead of by heartbeats.
* Unstaking maturity task will be processing up to 100 neurons in a single message, to avoid
  exceeding the instruction limit in a single execution.
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
