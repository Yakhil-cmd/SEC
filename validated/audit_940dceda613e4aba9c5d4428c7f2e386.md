Audit Report

## Title
Unbounded Stable-Memory Scan in `get_neuron_ids_ready_to_finalize` Can Exhaust Instruction Limit in NNS Governance Maturity-Disbursement Timer Task — (`rs/nns/governance/src/maturity_disbursement_index.rs`)

## Summary
`MaturityDisbursementIndex::get_neuron_ids_ready_to_finalize` performs an unbounded `range(..=max_key).collect()` over a `StableBTreeMap`, materialising every overdue entry into a heap `BTreeSet` in a single synchronous call. Because the IC enforces a hard 40 B instruction limit per message and stable-memory traversals are instruction-expensive, a sufficiently large index can cause the `FinalizeMaturityDisbursementsTask` timer to trap on every invocation, permanently stalling all maturity-disbursement finalisation. An efficient O(1) alternative (`get_next_entry()`) already exists in the same file but is not used in the hot path.

## Finding Description

**Root cause — unbounded collect in `get_neuron_ids_ready_to_finalize`** [1](#0-0) 

The method issues `range(..=max_key)` and calls `.collect()` with no upper bound, scanning every `(timestamp, neuron_id)` pair whose timestamp is ≤ `now_seconds`. The result is a `BTreeSet<NeuronId>` allocated entirely on the heap.

**Efficient alternative already present but unused in the hot path** [2](#0-1) 

`get_next_entry()` calls `first_key_value()` — an O(1) stable-BTreeMap operation — and returns only the minimum-timestamp entry. It is used in `get_delay_until_next_finalization` for scheduling but not in the finalization path itself.

**Call chain that triggers the scan**

`next_maturity_disbursement_to_finalize` calls `get_neuron_ids_ready_to_finalize_maturity_disbursement` and then iterates the resulting set to find the first non-locked neuron: [3](#0-2) 

This is called synchronously (before any `await`) inside `try_finalize_maturity_disbursement`, which is called by `finalize_maturity_disbursement`, which is called by `FinalizeMaturityDisbursementsTask::execute`: [4](#0-3) 

The task is scheduled unconditionally at canister startup: [5](#0-4) 

**Index accumulation path (unprivileged)**

Any neuron controller can call `disburse_maturity`, which inserts one `(finalize_timestamp, neuron_id)` entry per call. Up to `MAX_NUM_DISBURSEMENTS = 10` entries per neuron are permitted: [6](#0-5) 

Entries are only removed when a disbursement is finalised, so the index grows monotonically until finalisation catches up.

**Instruction limit** [7](#0-6) 

The hard per-message limit is 40 B instructions. Stable-BTreeMap node traversals are instruction-expensive; at sufficient index size the synchronous `collect()` exhausts this budget, causing the message to trap. Because `RecurringAsyncTask` reschedules only after a successful return from `execute`, a trap prevents rescheduling and permanently stalls the task.

**Why existing guards are insufficient**

The `MAX_NUM_DISBURSEMENTS = 10` cap limits entries per neuron but does not bound the total index size across all neurons. With ~500 000 NNS neurons, the theoretical maximum index size is ~5 000 000 entries. No per-invocation batch limit analogous to the `max_num_neurons` parameter used by `unstake_maturity_of_dissolved_neurons` exists for this path: [8](#0-7) 

## Impact Explanation

If the index grows large enough to exhaust the 40 B instruction budget, every invocation of `FinalizeMaturityDisbursementsTask` traps. The timer never reschedules. All pending maturity disbursements across all NNS neurons are frozen: no ICP is minted, no disbursement record is popped, and the governance canister's maturity-disbursement subsystem is effectively DoS'd until a canister upgrade prunes or restructures the index. This matches the allowed impact: **High — Application/platform-level DoS with concrete user and protocol harm** (no ICP disbursed to any neuron holder until an upgrade is deployed).

## Likelihood Explanation

**Organic growth:** As `disburse_maturity` adoption grows and reward events accumulate, the index grows without any adversarial action. A large fraction of the ~500 000 existing neurons accumulating multiple simultaneous pending disbursements (e.g., after a period of high reward distribution) could push the index into the danger zone.

**Deliberate attack:** An adversary controlling many neurons can maximise the index by calling `disburse_maturity` up to 10 times per neuron and delaying finalisation. The cost is high (requires earned maturity, not purchasable on demand), making a deliberate attack expensive but not impossible for a well-funded actor.

Likelihood is **low-to-medium**: not trivially exploitable today, but a realistic operational risk as NNS adoption grows.

## Recommendation

Replace the full `collect()` scan with an incremental approach using the already-implemented `get_next_entry()`:

1. Call `get_next_entry()` to fetch the minimum-timestamp entry in O(1).
2. If that neuron is locked, iterate forward one entry at a time using `range((Excluded(current_key), Unbounded)).next()` until a non-locked neuron is found or a soft instruction-count budget is reached.
3. Alternatively, add a `get_first_n_ready(n, now)` method returning at most `n` entries and call it with a small constant (e.g., 100), mirroring the `max_num_neurons` bound already used by `unstake_maturity_of_dissolved_neurons`.

## Proof of Concept

```
// Invariant/integration test plan (PocketIC or canister-level benchmark):
// 1. Create N neurons each with 10 pending disbursements (finalize_ts <= now).
//    Total index entries = 10 * N.
// 2. Advance time past all finalize timestamps.
// 3. Trigger FinalizeMaturityDisbursementsTask::execute() via timer.
// 4. Observe: at small N (e.g., 1 000) the task succeeds.
//    At large N (e.g., 50 000+) the task traps with InstructionLimitExceeded.
// 5. Confirm the timer is not rescheduled after the trap (no further
//    disbursements are processed).
//
// Alternatively, use canbench-rs (already wired in neuron_store.rs) to
// benchmark get_neuron_ids_ready_to_finalize at varying index sizes and
// extrapolate the instruction count to the 40 B limit.
```

### Citations

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

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L38-40)
```rust
/// The maximum number of disbursements in a neuron. This makes it possible to do daily
/// disbursements after every reward event (as 10 > 7).
const MAX_NUM_DISBURSEMENTS: usize = 10;
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

**File:** rs/nns/governance/src/timer_tasks/finalize_maturity_disbursements.rs (L22-24)
```rust
    async fn execute(self) -> (Duration, Self) {
        let delay = finalize_maturity_disbursement(self.governance).await;
        (delay, self)
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

**File:** rs/nns/governance/src/neuron_store.rs (L632-636)
```rust
    pub fn unstake_maturity_of_dissolved_neurons(
        &mut self,
        now_seconds: u64,
        max_num_neurons: usize,
    ) {
```
