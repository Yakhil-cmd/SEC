### Title
Malicious Canister Can Spam Minimum-Timeout Best-Effort Messages to Displace Legitimate Messages via Load Shedding - (File: `rs/replicated_state/src/replicated_state.rs`)

---

### Summary

The IC's best-effort inter-canister messaging system enforces a subnet-wide memory cap (`SUBNET_BEST_EFFORT_MESSAGE_MEMORY_CAPACITY = 5 GiB`) and sheds the **largest** best-effort messages when the cap is exceeded. There is no minimum timeout enforced for best-effort calls — only a maximum of 300 seconds. A malicious canister can flood legitimate canisters with many small best-effort requests carrying a 1-second timeout, filling the memory cap and triggering load shedding of legitimate large messages. The attacker's messages then expire in the next round at no further cost, while the legitimate messages are permanently lost.

---

### Finding Description

**Root cause 1 — No minimum timeout for best-effort calls:**

In `rs/embedders/src/wasmtime_embedder/system_api.rs`, `ic0_call_with_best_effort_response` only enforces an **upper** bound on the timeout:

```rust
let bounded_timeout = std::cmp::min(timeout_seconds, MAX_CALL_TIMEOUT_SECONDS);
request.set_timeout(bounded_timeout);
```

`MAX_CALL_TIMEOUT_SECONDS = 300`. There is no lower bound. A canister can pass `timeout_seconds = 1`, producing a deadline of `current_time + 1 second`. [1](#0-0) [2](#0-1) 

**Root cause 2 — Load shedding evicts the largest messages globally:**

At the end of every round, `enforce_best_effort_message_limit` is called in `rs/messaging/src/state_machine.rs`:

```rust
state_after_stream_builder.enforce_best_effort_message_limit(
    self.best_effort_message_memory_capacity, &self.metrics,
);
``` [3](#0-2) 

The implementation in `rs/replicated_state/src/replicated_state.rs` selects the canister with the **highest** best-effort memory usage and calls `shed_largest_message()` on it, which removes the **globally largest** best-effort message from that canister's pool: [4](#0-3) 

`shed_largest_message` in `rs/replicated_state/src/canister_state/queues/message_pool.rs` pops the maximum key from the `size_queue` (ordered by byte size): [5](#0-4) 

**The attack chain:**

1. Attacker deploys N canisters (to bypass any per-canister rate limits).
2. Each attacker canister sends many **small** best-effort requests with `timeout_seconds = 1` to one or more **legitimate** canisters.
3. These small messages are inducted into the legitimate canisters' input queues and counted toward their best-effort memory usage.
4. Total subnet best-effort memory exceeds `SUBNET_BEST_EFFORT_MESSAGE_MEMORY_CAPACITY = 5 GiB`. [6](#0-5) 
5. `enforce_best_effort_message_limit` fires and sheds the **largest** messages from the most memory-heavy canister — which are the **legitimate** large messages, not the attacker's small ones.
6. In the next round, `expire_messages` removes the attacker's 1-second-deadline messages. The attacker's messages are gone; the legitimate large messages are permanently lost.

The `size_queue` in `MessagePool` is keyed by `(usize, Id)` — byte size ascending — so `max_key()` always returns the largest message, which is the legitimate one when the attacker deliberately sends smaller messages. [7](#0-6) 

---

### Impact Explanation

Legitimate best-effort inter-canister messages are permanently dropped (converted to `SYS_UNKNOWN` reject responses). Any canister-to-canister workflow relying on best-effort messaging — cross-subnet DeFi calls, SNS governance downstream calls, ckBTC/ckETH minter interactions — can be disrupted. The receiving canister's callback fires with a `SYS_UNKNOWN` reject, which may cause incorrect state transitions or loss of in-flight cycles attached to those messages. The 5 GiB cap is a subnet-wide resource, so the attack affects all canisters on the targeted subnet simultaneously.

---

### Likelihood Explanation

The attacker only needs cycles sufficient to send inter-canister messages. Since the attacker's messages expire in 1 second, the cycles cost per attack wave is minimal and the attack can be repeated every round (~1 second). No privileged access, governance majority, or threshold corruption is required. Any deployed canister can call `ic0_call_with_best_effort_response(1)` and `ic0_call_perform`. Multiple attacker canisters can be used to amplify throughput and bypass any implicit per-canister throttling.

---

### Recommendation

1. **Enforce a minimum timeout for best-effort calls.** Add a lower bound (e.g., 10 seconds) alongside the existing upper bound in `ic0_call_with_best_effort_response`:
   ```rust
   const MIN_CALL_TIMEOUT_SECONDS: u32 = 10;
   let bounded_timeout = timeout_seconds
       .max(MIN_CALL_TIMEOUT_SECONDS)
       .min(MAX_CALL_TIMEOUT_SECONDS);
   ```
   This raises the cost of the attack: the attacker's messages occupy memory for at least 10 rounds, increasing the cycles cost per attack wave.

2. **Consider deadline proximity in the shedding order.** Rather than shedding purely by size, prefer shedding messages whose deadlines are nearest to expiry first. Messages about to expire naturally are the cheapest to shed and the least disruptive to legitimate traffic.

3. **Rate-limit best-effort message injection per sender canister.** Track per-sender best-effort message counts/bytes in the induction layer and reject or throttle senders that exceed a per-round quota.

---

### Proof of Concept

```
Round N:
  Attacker canister A1 calls ic0_call_with_best_effort_response(1) and sends
  1000 × 5 KB best-effort requests to LegitCanister (total: ~5 MB from A1).
  Attacker canisters A2..A1000 do the same (total: ~5 GB across all senders).

  LegitCanister's input queue now holds:
    - 1,000,000 × 5 KB attacker messages  (small, deadline = now+1s)
    - 100 × 50 MB legitimate messages      (large, deadline = now+200s)

  enforce_best_effort_message_limit fires:
    LegitCanister has highest memory usage.
    shed_largest_message() repeatedly removes the 50 MB legitimate messages.
    All 100 legitimate messages are shed → SYS_UNKNOWN reject to their senders.

Round N+1:
  expire_messages() removes all attacker messages (deadline < now).
  Attacker's memory footprint: 0.
  Legitimate messages: permanently gone.
```

The attacker's cost is proportional to the number of small messages sent (cycles for `call_perform`), which is far less than the value of the disrupted legitimate traffic. The attack is repeatable every round.

### Citations

**File:** rs/embedders/src/wasmtime_embedder/system_api.rs (L63-65)
```rust
/// Upper bound on `timeout` when using calls with
/// best-effort responses represented in seconds.
pub const MAX_CALL_TIMEOUT_SECONDS: u32 = 300;
```

**File:** rs/embedders/src/wasmtime_embedder/system_api.rs (L4196-4200)
```rust
                Some(request) => {
                    let bounded_timeout =
                        std::cmp::min(timeout_seconds, MAX_CALL_TIMEOUT_SECONDS);
                    request.set_timeout(bounded_timeout);
                    Ok(())
```

**File:** rs/messaging/src/state_machine.rs (L265-270)
```rust
        // Shed enough messages to stay below the best-effort message memory limit.
        let shed_messages_timer = self.metrics.start_phase_timer(PHASE_SHED_MESSAGES);
        state_after_stream_builder.enforce_best_effort_message_limit(
            self.best_effort_message_memory_capacity,
            &self.metrics,
        );
```

**File:** rs/replicated_state/src/replicated_state.rs (L1333-1363)
```rust
        while memory_usage > limit && !priority_queue.is_empty() {
            let (memory_usage_before, canister_id) = priority_queue.pop_last().unwrap();

            let (message_shed, memory_usage_after) = if canister_id.get()
                == self.metadata.own_subnet_id.get()
            {
                // Shed from the subnet queues.
                let message_shed = self.subnet_queues.shed_largest_message(
                    &canister_id,
                    &self.canister_states,
                    &mut self.refunds,
                    metrics,
                );
                let memory_usage_after =
                    (self.subnet_queues.best_effort_message_memory_usage() as u64).into();
                (message_shed, memory_usage_after)
            } else {
                // Shed from a canister's queues: remove the canister, shed its largest message,
                // replace it.
                let mut canister = self.canister_states.remove(&canister_id).unwrap();
                let canister_state = Arc::make_mut(&mut canister);
                let message_shed = canister_state.system_state.shed_largest_message(
                    &canister_id,
                    &self.canister_states,
                    &mut self.refunds,
                    metrics,
                );
                let memory_usage_after = canister.system_state.best_effort_message_memory_usage();
                self.canister_states.insert(canister);
                (message_shed, memory_usage_after)
            };
```

**File:** rs/replicated_state/src/canister_state/queues/message_pool.rs (L392-396)
```rust
    /// Load shedding priority queue. Holds all best-effort messages, ordered by
    /// size.
    ///
    /// Message IDs break ties, ensuring deterministic ordering.
    size_queue: MutableIntMap<(usize, Id), ()>,
```

**File:** rs/replicated_state/src/canister_state/queues/message_pool.rs (L674-688)
```rust
    pub(super) fn shed_largest_message(&mut self) -> Option<(SomeReference, RequestOrResponse)> {
        if let Some(&(size_bytes, id)) = self.size_queue.max_key() {
            self.size_queue.remove(&(size_bytes, id)).unwrap();
            debug_assert_eq!(Class::BestEffort, id.class());

            let msg = self.take_impl(id).unwrap();
            self.remove_from_deadline_queue(id, &msg);

            debug_assert_eq!(Ok(()), self.check_invariants());
            return Some((id.into(), msg));
        }

        // Nothing to shed.
        None
    }
```

**File:** rs/config/src/execution_environment.rs (L58-62)
```rust
/// on a given subnet at the end of a round.
///
/// During the round, the best-effort message memory usage may exceed the limit,
/// but the constraint is restored at the end of the round by shedding messages.
const SUBNET_BEST_EFFORT_MESSAGE_MEMORY_CAPACITY: NumBytes = NumBytes::new(5 * GIB);
```
