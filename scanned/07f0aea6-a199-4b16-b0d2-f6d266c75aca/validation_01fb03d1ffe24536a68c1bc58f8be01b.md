### Title
Subnet-Wide Execution DoS via Heap Delta Budget Exhaustion in a Single Round — (`rs/execution_environment/src/scheduler.rs`)

---

### Summary

A canister with sufficient memory can dirty enough Wasm pages in a single round to push `state.metadata.heap_delta_estimate` above `scheduled_heap_delta_limit` for all subsequent rounds in the checkpoint interval, causing `execute_round` to return early without executing any canister messages. There is no per-canister heap delta cap, so a single unprivileged canister can consume the entire subnet's heap delta budget and block all other canisters for up to one full checkpoint interval (~500 rounds, ~10 minutes on mainnet).

---

### Finding Description

**`scheduled_heap_delta_limit`** (lines 2079–2113) computes a smoothly increasing budget:

```
scheduled_heap_delta_limit = subnet_heap_delta_capacity
    - heap_delta_capacity_minus_initial_reserve * remaining_rounds / current_interval_length
```

At the very first round of an interval (`remaining_rounds == current_interval_length`), this collapses to:

```
scheduled_heap_delta_limit = heap_delta_initial_reserve
```

which is `min(config.heap_delta_initial_reserve, subnet_heap_delta_capacity / 2)`. [1](#0-0) 

**`execute_round`** (lines 1351–1367) checks this limit before any canister execution:

```rust
if state.metadata.heap_delta_estimate >= scheduled_heap_delta_limit {
    // warn! ...
    self.finish_round(&mut state, current_round_type);
    return state;   // ← all canister messages skipped
}
``` [2](#0-1) 

`heap_delta_estimate` is a **subnet-wide accumulator** with no per-canister component and no per-round cap. If an attacker canister generates `X` bytes of heap delta in round 1, every subsequent round `k` is skipped while:

```
X  >=  subnet_heap_delta_capacity
     - heap_delta_capacity_minus_initial_reserve * remaining_rounds / interval_length
```

Solving for the number of blocked rounds:

```
blocked_rounds ≈ interval_length × (X - heap_delta_initial_reserve)
                 / heap_delta_capacity_minus_initial_reserve
```

| Heap delta generated (X) | Rounds blocked (interval = 500) |
|---|---|
| 50 MB (2× reserve) | ~4 rounds |
| 1 GB | ~164 rounds |
| 3 GB (≈ capacity) | ~499 rounds (entire interval) |

A canister with 3 GB of Wasm memory can dirty all of it in a single round using sequential `memory.store` instructions; the per-round instruction limit (~2 billion) is not the binding constraint — memory size is. This generates ~3 GB of heap delta, exhausting the entire interval budget in one shot. [3](#0-2) 

---

### Impact Explanation

- **All canisters on the subnet** stop executing messages for up to one full checkpoint interval (~10 minutes).
- The attack is repeatable: the attacker re-triggers it at the start of each new checkpoint interval.
- Subnet messages (consensus queue, raw_rand) are processed **before** the heap delta check, so the subnet itself is not fully halted, but all user-canister execution is blocked.
- No per-canister fairness mechanism exists to isolate the offending canister's contribution to `heap_delta_estimate`.

---

### Likelihood Explanation

- **Attacker capability**: Any canister developer can deploy a canister with up to 4 GB of Wasm memory and send a single ingress message that dirties the entire allocation.
- **Cost**: Cycles are charged for instruction execution, not for dirty pages directly. The cost of 2 billion instructions is non-trivial but affordable for a motivated attacker.
- **No privileged access required**: Standard canister deployment and ingress message submission.
- **Detectability**: The warn log fires, but by then the damage is done for the interval.

---

### Recommendation

1. **Add a per-canister heap delta cap per round**: Track each canister's contribution to `heap_delta_estimate` and refuse to schedule a canister that would push its own share above a per-canister limit.
2. **Attribute heap delta to the generating canister**: When `heap_delta_estimate` exceeds the scheduled limit, skip only the offending canister rather than the entire round.
3. **Charge cycles proportional to dirty pages**: Make the economic cost of dirtying large amounts of memory prohibitive.
4. **Add a per-round heap delta cap**: Independently of the interval budget, cap how much heap delta any single round can accumulate before stopping further canister execution within that round (rather than skipping the entire next round).

---

### Proof of Concept

```rust
// Scheduler test sketch
let mut test = SchedulerTestBuilder::new()
    .with_subnet_heap_delta_capacity(NumBytes::from(3 * 1024 * 1024 * 1024)) // 3 GB
    .with_heap_delta_initial_reserve(NumBytes::from(25 * 1024 * 1024))        // 25 MB
    .build();

// Attacker canister: dirties 3 GB of pages in one message
let attacker = test.create_canister_with(
    Cycles::new(1_000_000_000_000),
    ComputeAllocation::try_from(50).unwrap(),
    MemoryAllocation::try_from(3 * 1024 * 1024 * 1024).unwrap(),
    None,
);
// victim canister with pending messages
let victim = test.create_canister();
test.send_ingress(victim, ingress(1));

// Round 1: attacker dirties 3 GB → heap_delta_estimate ≈ 3 GB
test.execute_round_with_dirty_pages(attacker, 3 * 1024 * 1024 * 1024 / 65536);

// Rounds 2–499: assert victim's message is NEVER executed
for _ in 2..500 {
    test.execute_round();
    assert_eq!(test.ingress_queue_size(victim), 1, "victim blocked");
}
```

The assertion at the end demonstrates that the victim canister's messages are not executed for the remainder of the checkpoint interval, violating the invariant that a single canister must not block all others for more than one round. [4](#0-3)

### Citations

**File:** rs/execution_environment/src/scheduler.rs (L1345-1367)
```rust
            let scheduled_heap_delta_limit = scheduled_heap_delta_limit(
                current_round,
                round_summary,
                subnet_heap_delta_capacity,
                heap_delta_initial_reserve,
            );
            if state.metadata.heap_delta_estimate >= scheduled_heap_delta_limit {
                warn!(
                    round_log,
                    "At Round {} @ time {}, current heap delta estimate {} \
                        exceeds scheduled limit {} out of {}, so not executing any messages.",
                    current_round,
                    state.time(),
                    state.metadata.heap_delta_estimate,
                    scheduled_heap_delta_limit,
                    subnet_heap_delta_capacity,
                );
                self.finish_round(&mut state, current_round_type);
                self.metrics
                    .round_skipped_due_to_current_heap_delta_above_limit
                    .inc();
                return state;
            }
```

**File:** rs/execution_environment/src/scheduler.rs (L2079-2113)
```rust
fn scheduled_heap_delta_limit(
    current_round: ExecutionRound,
    round_summary: Option<ExecutionRoundSummary>,
    subnet_heap_delta_capacity: NumBytes,
    heap_delta_initial_reserve: NumBytes,
) -> NumBytes {
    let Some(round_summary) = round_summary else {
        // This should happen only in tests.
        return subnet_heap_delta_capacity;
    };
    let next_checkpoint_round = round_summary.next_checkpoint_round;
    // Plus one is because the interval length is normally 499, not 500.
    let current_interval_length = round_summary
        .current_interval_length
        .get()
        .saturating_add(1);
    let remaining_rounds = next_checkpoint_round
        .get()
        .saturating_sub(current_round.get());

    // The initial reserve is always available, so it should not be scaled.
    let heap_delta_capacity_minus_initial_reserve = subnet_heap_delta_capacity
        .get()
        .saturating_sub(heap_delta_initial_reserve.get());
    // The rest of the heap delta capacity is distributed across remaining rounds.
    let remaining_rounds = remaining_rounds.min(current_interval_length);
    let remaining_heap_delta_reserve = heap_delta_capacity_minus_initial_reserve
        .saturating_mul(remaining_rounds)
        .saturating_div(current_interval_length);

    // The scheduled limit is the capacity minus reserve for the remaining rounds.
    subnet_heap_delta_capacity
        .get()
        .saturating_sub(remaining_heap_delta_reserve)
        .into()
```
