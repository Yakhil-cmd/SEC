### Title
Unbounded Stable-Memory Scan in `get_neuron_ids_ready_to_finalize` Can Exhaust Instruction Limit in NNS Governance Maturity-Disbursement Timer Task — (`rs/nns/governance/src/maturity_disbursement_index.rs`)

---

### Summary

The NNS Governance canister's `FinalizeMaturityDisbursementsTask` timer calls `get_neuron_ids_ready_to_finalize`, which performs an unbounded range scan over a `StableBTreeMap` and materialises every overdue entry into a heap `BTreeSet` in a single synchronous call. Because each NNS neuron holder can create up to 10 pending disbursements, a sufficiently large population of neurons with overdue disbursements can cause this scan to exhaust the IC's per-message instruction limit (40 B instructions), trapping the timer task and permanently stalling all maturity-disbursement finalisation.

---

### Finding Description

**Entry point — `disburse_maturity` (unprivileged ingress)**

Any NNS neuron controller can call `manage_neuron` → `disburse_maturity`, which calls `initiate_maturity_disbursement`. Each successful call appends one `MaturityDisbursement` record to the neuron and inserts a `(finalize_disbursement_timestamp_seconds, neuron_id)` key into the `MaturityDisbursementIndex` stable BTreeMap. Up to `MAX_NUM_DISBURSEMENTS = 10` entries per neuron are allowed. [1](#0-0) [2](#0-1) 

**Index accumulation**

The `MaturityDisbursementIndex` stores `(timestamp, neuron_id) → ()` pairs in a `StableBTreeMap`. Every `disburse_maturity` call inserts one entry; entries are only removed when the disbursement is finalised. [3](#0-2) [4](#0-3) 

**Unbounded scan — the root cause**

`get_neuron_ids_ready_to_finalize` issues a `range(..=max_key)` query and calls `.collect()` on the entire result, materialising every overdue entry from stable memory into a heap `BTreeSet` in one synchronous call with no upper bound: [5](#0-4) 

This is called from `next_maturity_disbursement_to_finalize`, which only needs the *first* non-locked neuron but forces a full scan to find it: [6](#0-5) 

**Timer task that executes the scan**

`FinalizeMaturityDisbursementsTask` is scheduled unconditionally at canister startup and calls `finalize_maturity_disbursement` → `try_finalize_maturity_disbursement` → `next_maturity_disbursement_to_finalize` on every invocation: [7](#0-6) [8](#0-7) 

**Instruction-limit context**

The IC enforces a hard per-message instruction limit of 40 B instructions for update/timer executions. Each stable-BTreeMap node traversal costs thousands of instructions; a scan over tens of thousands of entries can exhaust this budget. [9](#0-8) 

An efficient alternative already exists — `get_next_entry()` returns only the minimum-timestamp entry in O(1) — but it is not used in the hot path: [10](#0-9) 

---

### Impact Explanation

If the number of overdue disbursement index entries grows large enough, every invocation of `FinalizeMaturityDisbursementsTask` traps with `InstructionLimitExceeded`. Because the timer reschedules itself only after a successful return, the task enters a permanent failure loop. All pending maturity disbursements across all NNS neurons are frozen: no ICP is minted, no disbursement record is popped, and the governance canister's maturity-disbursement subsystem is effectively DoS'd until the index is pruned by other means (e.g., a canister upgrade).

---

### Likelihood Explanation

**Deliberate attack:** An adversary controlling many neurons (or coordinating with many users) can create up to 10 disbursements per neuron. Stable-BTreeMap range scans over ~40 000–400 000 entries are sufficient to exhaust the 40 B instruction budget (depending on per-node cost). At 10 ICP of maturity per neuron × 40 000 neurons = 400 000 ICP, the cost is high but not impossible for a well-funded actor, and no flash-loan shortcut exists because maturity must be earned over time.

**Organic growth:** As the NNS matures and `disburse_maturity` becomes widely used, the index will grow organically. If a large fraction of the ~500 000 existing neurons accumulate multiple pending disbursements simultaneously (e.g., after a period of high reward distribution), the scan cost could cross the instruction limit without any deliberate attack.

Likelihood is **low-to-medium**: not trivially exploitable today, but a realistic operational risk as adoption grows.

---

### Recommendation

Replace the full `collect()` scan with an incremental cursor approach:

1. Use `get_next_entry()` (already implemented) to fetch the minimum-timestamp entry.
2. If that neuron is locked, iterate forward one entry at a time using `range((Excluded(current_key), Unbounded)).next()` until a non-locked neuron is found or the soft instruction limit is reached.
3. Alternatively, add a `get_first_n_ready(n, now)` method that returns at most `n` entries, and call it with a small constant (e.g., 100) so the scan cost is bounded per timer invocation.

This mirrors the batching fix suggested in the Kinetiq report and is consistent with how other NNS timer tasks (e.g., `unstake_maturity_of_dissolved_neurons`) already apply a `max_num_neurons` bound. [11](#0-10) 

---

### Proof of Concept

```
// Pseudocode — not a runnable test
// Assume N neurons each with 10 pending disbursements (finalize_ts <= now)
// Total index entries = 10 * N

// Timer fires:
FinalizeMaturityDisbursementsTask::execute()
  -> finalize_maturity_disbursement()
  -> try_finalize_maturity_disbursement()
  -> next_maturity_disbursement_to_finalize()
  -> get_neuron_ids_ready_to_finalize_maturity_disbursement(now)
  -> MaturityDisbursementIndex::get_neuron_ids_ready_to_finalize(now)
       // range(..=(now, u64::MAX)).collect()  ← scans all 10*N entries
       // At ~10 000 instructions/entry and N = 4 000 neurons:
       //   10 * 4 000 * 10 000 = 400 000 000 instructions  (within limit)
       // At N = 400 000 neurons:
       //   10 * 400 000 * 10 000 = 40 000 000 000 instructions  (≥ 40 B limit → TRAP)

// Result: InstructionLimitExceeded; timer never reschedules; all disbursements frozen.
```

### Citations

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L38-40)
```rust
/// The maximum number of disbursements in a neuron. This makes it possible to do daily
/// disbursements after every reward event (as 10 > 7).
const MAX_NUM_DISBURSEMENTS: usize = 10;
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L306-319)
```rust
    if num_disbursements >= MAX_NUM_DISBURSEMENTS {
        return Err(InitiateMaturityDisbursementError::TooManyDisbursements);
    }

    let disbursement_in_progress = MaturityDisbursement {
        destination: Some(destination),
        amount_e8s: disbursement_maturity_e8s,
        timestamp_of_disbursement_seconds,
        finalize_disbursement_timestamp_seconds,
    };

    neuron_store
        .with_neuron_mut(id, |neuron| {
            neuron.add_maturity_disbursement_in_progress(disbursement_in_progress);
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

**File:** rs/nns/governance/src/maturity_disbursement_index.rs (L13-19)
```rust
pub struct MaturityDisbursementIndex<M: Memory> {
    // Conceptually, it is possible to use StableMinHeap here. However, `impl NeuronIndex`  requires
    // the index to be able to remove any entries associated with a neuron, which is a bit
    // restrictive since we don't expect any disbursement to be removed. On the other hand, using
    // StableBTreeMap achieves similar performance.
    finalization_timestamp_neuron_id_to_null: StableBTreeMap<(TimestampSeconds, NeuronId), (), M>,
}
```

**File:** rs/nns/governance/src/maturity_disbursement_index.rs (L43-59)
```rust
    pub fn add_neuron_id_finalization_timestamps(
        &mut self,
        neuron_id: NeuronId,
        finalization_timestamps: BTreeSet<TimestampSeconds>,
    ) -> Vec<TimestampSeconds> {
        let mut already_present_timestamps = Vec::new();
        for finalization_timestamp in finalization_timestamps {
            let already_present = self
                .finalization_timestamp_neuron_id_to_null
                .insert((finalization_timestamp, neuron_id), ())
                .is_some();
            if already_present {
                already_present_timestamps.push(finalization_timestamp);
            }
        }
        already_present_timestamps
    }
```

**File:** rs/nns/governance/src/maturity_disbursement_index.rs (L82-91)
```rust
    pub fn get_neuron_ids_ready_to_finalize(
        &self,
        now_seconds: TimestampSeconds,
    ) -> BTreeSet<NeuronId> {
        let max_key = (now_seconds, u64::MAX);
        self.finalization_timestamp_neuron_id_to_null
            .range(..=max_key)
            .map(|((_, neuron_id), _)| neuron_id)
            .collect()
    }
```

**File:** rs/nns/governance/src/maturity_disbursement_index.rs (L93-100)
```rust
    /// Returns the next entry of the index.
    pub fn get_next_entry(&self) -> Option<(TimestampSeconds, NeuronIdProto)> {
        self.finalization_timestamp_neuron_id_to_null
            .first_key_value()
            .map(|((finalization_timestamp, neuron_id), _)| {
                (finalization_timestamp, NeuronIdProto::from_u64(neuron_id))
            })
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

**File:** rs/nns/governance/src/timer_tasks/mod.rs (L42-43)
```rust
    FinalizeMaturityDisbursementsTask::new(&GOVERNANCE).schedule(&METRICS_REGISTRY);
    UnstakeMaturityOfDissolvedNeuronsTask::new(&GOVERNANCE).schedule(&METRICS_REGISTRY);
```

**File:** rs/config/src/subnet_config.rs (L36-36)
```rust
pub(crate) const MAX_INSTRUCTIONS_PER_MESSAGE: NumInstructions = NumInstructions::new(40 * B);
```

**File:** rs/nns/governance/src/neuron_store.rs (L630-636)
```rust
    /// When a neuron is finally dissolved, if there is any staked maturity it is moved to regular maturity
    /// which can be spawned (and is modulated).
    pub fn unstake_maturity_of_dissolved_neurons(
        &mut self,
        now_seconds: u64,
        max_num_neurons: usize,
    ) {
```
