### Title
Unbounded `IngressQueue` Accumulation via Free-Cost Subnet Management Messages Bypassing `ingress_history_max_messages` Limit - (File: `rs/messaging/src/scheduling/valid_set_rule.rs`)

---

### Summary

The IC's `ValidSetRuleImpl::enqueue()` enforces `ingress_history_max_messages` only for non-system subnets and only for messages with a fee. Free-cost subnet management messages (e.g., `update_settings` with a small payload) bypass this count check entirely and are pushed unconditionally into the `IngressQueue`, which itself has **no upper bound** on the number of messages it can store. This is the direct analog of the reported mempool bug: a category of messages (pending/free-cost) bypasses the configured pool size limit and accumulates without bound.

---

### Finding Description

In `rs/messaging/src/scheduling/valid_set_rule.rs`, the `enqueue()` function first checks the ingress history limit:

```rust
if state.metadata.own_subnet_type != SubnetType::System
    && state.metadata.ingress_history.len() >= self.ingress_history_max_messages
{
    return Err(IngressInductionError::IngressHistoryFull { ... });
}
``` [1](#0-0) 

After this check, the function computes the induction cost. When the cost is `IngressInductionCost::Free` (which occurs for `update_settings` with a small payload, and other subnet methods where `effective_canister_id` resolves to `None`), the message is pushed directly into the queue **without any further capacity check**:

```rust
IngressInductionCost::Free => {
    // Only subnet methods can be free. These are enqueued directly.
    assert!(ingress.is_addressed_to_subnet());
    state.push_ingress(ingress)
}
``` [2](#0-1) 

The `IngressQueue` that receives these messages is explicitly documented as having **no upper bound**:

```rust
/// Representation of the Ingress queue. There is no upper bound on
/// the number of messages it can store.
``` [3](#0-2) 

Its `push()` method unconditionally appends:

```rust
pub(super) fn push(&mut self, msg: Ingress) {
    ...
    receiver_ingress_queue.push_back(Arc::new(msg));
    self.total_ingress_count += 1;
}
``` [4](#0-3) 

The `ingress_history_max_messages` guard is also bypassed for system subnets entirely by design:

```rust
if state.metadata.own_subnet_type != SubnetType::System
    && state.metadata.ingress_history.len() >= self.ingress_history_max_messages
``` [5](#0-4) 

The `ingress_induction_cost()` function returns `IngressInductionCost::Free` when `paying_canister` is `None`, which happens for `update_settings` with a small payload (delayed cost) and potentially other subnet methods:

```rust
None => IngressInductionCost::Free,
``` [6](#0-5) 

The `INGRESS_HISTORY_MAX_MESSAGES` constant is `2 * 1000 * 300 = 600,000` messages:

```rust
pub const INGRESS_HISTORY_MAX_MESSAGES: usize = 2 * 1000 * MAX_INGRESS_TTL.as_secs() as usize;
``` [7](#0-6) 

The throttle check at the HTTP endpoint (`exceeds_threshold`) only checks the **local node's own ingress pool** (validated + unvalidated sections), not the `IngressQueue` inside `ReplicatedState`. Once messages pass the ingress pool and are inducted into `ReplicatedState`, the `IngressQueue` has no capacity enforcement. [8](#0-7) 

---

### Impact Explanation

An unprivileged user can submit a large volume of `update_settings` management canister calls with small payloads (which qualify as free-cost) targeting any canister they control. Each such message passes the HTTP endpoint throttle (which is per-node and per-peer, not a global `IngressQueue` bound), gets included in consensus blocks (up to `MAX_INGRESS_MESSAGES_PER_BLOCK = 1000` per block), and is inducted into the `IngressQueue` without any count limit. The `IngressQueue` grows without bound in memory, consuming replica heap memory. On application subnets, the `ingress_history_max_messages` guard would eventually fire for fee-bearing messages, but free-cost messages bypass it. On system subnets (NNS), the guard is disabled entirely. Sustained flooding can exhaust replica memory, causing OOM crashes and halting block production — matching the report's impact of "flooding the mempool and preventing the inclusion of normal transactions."

---

### Likelihood Explanation

The attack requires only a valid IC identity and the ability to submit ingress messages to the HTTP endpoint. The `update_settings` method with a small payload is a standard, publicly accessible management canister call. The per-node ingress pool throttle (`ingress_pool_max_count`, `ingress_pool_max_bytes`) limits how many messages a single node will accept from a single peer, but an attacker can distribute submissions across multiple boundary nodes or use multiple identities. Each block can carry up to 1,000 ingress messages, and at ~1 block/second, this is 1,000 free-cost messages inducted per second with no `IngressQueue` cap. The likelihood is **medium**: it requires sustained submission volume but no privileged access.

---

### Recommendation

1. **Add a capacity bound to `IngressQueue`**: Introduce a configurable maximum message count (or byte limit) in `IngressQueue::push()` and return an error when exceeded, analogous to how `CanisterQueue` enforces `capacity`.

2. **Apply `ingress_history_max_messages` to free-cost messages**: The limit check in `enqueue()` should not be conditioned on the induction cost. Free-cost messages should count against the same limit.

3. **Apply the limit on system subnets with a higher (but finite) bound**: The unconditional bypass for `SubnetType::System` removes all protection on the NNS subnet.

---

### Proof of Concept

1. Obtain a canister ID `C` on an application subnet.
2. Construct `update_settings` ingress messages with a small payload (below the `is_delayed_ingress_induction_cost` threshold) targeting `IC_00` with `effective_canister_id = C`. These qualify as `IngressInductionCost::Free`.
3. Submit them continuously to the HTTP `/api/v2/canister/.../call` endpoint across multiple boundary nodes to avoid per-node throttling.
4. Each message passes `exceeds_threshold()` (per-node pool check), is included in consensus blocks, and is inducted via `ValidSetRuleImpl::enqueue()` → `IngressInductionCost::Free` branch → `state.push_ingress(ingress)` → `IngressQueue::push()` with no capacity check.
5. The `IngressQueue` grows unboundedly, consuming replica heap memory until OOM or severe performance degradation halts block production.

Key code path:
- `rs/http_endpoints/public/src/call.rs:230` — throttle check passes
- `rs/messaging/src/scheduling/valid_set_rule.rs:287-290` — free-cost branch, no limit check
- `rs/replicated_state/src/canister_state/queues/queue.rs:334-349` — unbounded `push()` [9](#0-8) [10](#0-9) [11](#0-10)

### Citations

**File:** rs/messaging/src/scheduling/valid_set_rule.rs (L250-256)
```rust
        if state.metadata.own_subnet_type != SubnetType::System
            && state.metadata.ingress_history.len() >= self.ingress_history_max_messages
        {
            return Err(IngressInductionError::IngressHistoryFull {
                capacity: self.ingress_history_max_messages,
            });
        }
```

**File:** rs/messaging/src/scheduling/valid_set_rule.rs (L286-291)
```rust
        match induction_cost {
            IngressInductionCost::Free => {
                // Only subnet methods can be free. These are enqueued directly.
                assert!(ingress.is_addressed_to_subnet());
                state.push_ingress(ingress)
            }
```

**File:** rs/replicated_state/src/canister_state/queues/queue.rs (L294-295)
```rust
/// Representation of the Ingress queue. There is no upper bound on
/// the number of messages it can store.
```

**File:** rs/replicated_state/src/canister_state/queues/queue.rs (L333-349)
```rust
    /// Pushes a new ingress message to the back of the queue.
    pub(super) fn push(&mut self, msg: Ingress) {
        let msg_size = Self::ingress_size_bytes(&msg);
        let receiver_ingress_queue = self.queues.entry(msg.effective_canister_id).or_default();

        if receiver_ingress_queue.is_empty() {
            self.schedule.push_back(msg.effective_canister_id);
            self.size_bytes += Self::PER_CANISTER_QUEUE_OVERHEAD_BYTES;
        }

        receiver_ingress_queue.push_back(Arc::new(msg));

        self.size_bytes += msg_size;
        debug_assert_eq!(Self::size_bytes(&self.queues), self.size_bytes);

        self.total_ingress_count += 1;
    }
```

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L595-596)
```rust
            None => IngressInductionCost::Free,
        }
```

**File:** rs/limits/src/lib.rs (L37-37)
```rust
pub const INGRESS_HISTORY_MAX_MESSAGES: usize = 2 * 1000 * MAX_INGRESS_TTL.as_secs() as usize;
```

**File:** rs/artifact_pool/src/ingress_pool.rs (L226-232)
```rust
    fn exceeds_limit(&self, peer_id: &NodeId) -> bool {
        let counters = self.unvalidated.peer_counters.get_counters(peer_id)
            + self.validated.peer_counters.get_counters(peer_id);

        counters.bytes > self.ingress_pool_max_bytes
            || counters.messages > self.ingress_pool_max_count
    }
```

**File:** rs/http_endpoints/public/src/call.rs (L229-236)
```rust
        // Load shed the request if the ingress pool is full.
        let ingress_pool_is_full = ingress_throttler.read().unwrap().exceeds_threshold();
        if ingress_pool_is_full {
            Err(HttpError {
                status: StatusCode::SERVICE_UNAVAILABLE,
                message: "Service is overloaded, try again later.".to_string(),
            })?;
        }
```
