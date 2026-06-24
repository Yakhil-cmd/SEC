### Title
Unbounded `IngressQueue` with Bypassed Capacity Guard on System Subnets Enables Ingress-Flooding DoS - (File: `rs/replicated_state/src/canister_state/queues/queue.rs`, `rs/messaging/src/scheduling/valid_set_rule.rs`)

---

### Summary

The `IngressQueue` data structure explicitly has no upper bound on the number of messages it can store. The only backstop against unbounded growth — the `ingress_history_max_messages` check in `ValidSetRule::enqueue()` — is unconditionally bypassed for System subnets (i.e., the NNS). An unprivileged attacker submitting many ingress messages to the NNS subnet can fill the inducted ingress queue, delaying or starving legitimate governance and management canister calls.

---

### Finding Description

`IngressQueue` is the per-canister queue that holds inducted ingress messages inside `CanisterQueues`. Its own documentation states:

> "There is no upper bound on the number of messages it can store." [1](#0-0) 

The `push()` method never returns an error and unconditionally accepts any message: [2](#0-1) 

The only protocol-level guard against unbounded induction is in `ValidSetRule::enqueue()`, which checks `ingress_history.len() >= ingress_history_max_messages`. However, this check is gated behind a `SubnetType::System` exclusion:

```rust
if state.metadata.own_subnet_type != SubnetType::System
    && state.metadata.ingress_history.len() >= self.ingress_history_max_messages
{
    return Err(IngressInductionError::IngressHistoryFull { ... });
}
``` [3](#0-2) 

For System subnets, this guard is never evaluated, so `push_ingress()` is called unconditionally: [4](#0-3) 

The upstream throttle at the HTTP endpoint (`exceeds_threshold()`) is a **global pool-level** soft limit with no per-sender quota: [5](#0-4) 

The `exceeds_limit()` per-peer check in `ingress_handler.rs` applies only to P2P gossip (keyed on `NodeId`), not to direct HTTP submissions from user principals: [6](#0-5) 

The `INGRESS_HISTORY_MAX_MESSAGES` constant (600,000 messages) is the intended ceiling for application subnets: [7](#0-6) 

System subnets have no equivalent ceiling.

---

### Impact Explanation

An attacker submitting a sustained stream of ingress messages to the NNS subnet (a System subnet) can grow the inducted `IngressQueue` without bound. Because the queue is processed in round-robin order across senders, a large attacker-controlled backlog delays execution of legitimate governance proposals, ICP ledger transfers, and management canister calls. The `IngressQueue` itself is held in replicated state, so memory growth is bounded only by the subnet's available heap, not by any protocol-enforced message count limit. This is a direct analog to the relayer queue flooding described in the external report.

---

### Likelihood Explanation

**Medium.** The attacker needs no special privileges — only the ability to submit valid signed ingress messages via the public HTTP API. The global ingress pool throttle (`exceeds_threshold()`) provides some resistance but can be circumvented by distributing submissions across multiple boundary nodes or by timing bursts to stay below the per-node threshold. Each block can induct up to `MAX_INGRESS_MESSAGES_PER_BLOCK = 1000` messages: [8](#0-7) 

At one block per second, an attacker can induct up to 1,000 messages/second into the unbounded queue. Sustaining this for minutes produces a backlog that delays legitimate users by an unbounded amount of time.

---

### Recommendation

- **Short term:** Apply the `ingress_history_max_messages` guard to System subnets as well, or introduce a separate per-canister ingress queue depth limit enforced inside `IngressQueue::push()` that returns an error on overflow.
- **Long term:** Introduce per-sender (principal-level) rate limiting at the HTTP ingress endpoint, analogous to the per-peer `exceeds_limit()` check that already exists for P2P gossip, to prevent a single identity or coordinated set of identities from monopolizing the ingress queue.

---

### Proof of Concept

1. Attacker generates N unique `SignedIngress` messages targeting any canister on the NNS subnet, each with a valid expiry within `MAX_INGRESS_TTL`.
2. Attacker submits them via the public `/api/v2/canister/{id}/call` endpoint. The global `exceeds_threshold()` check is the only gate; it can be bypassed by distributing across boundary nodes.
3. Consensus includes up to 1,000 messages per block. `ValidSetRule::induct_messages()` is called each round.
4. Inside `enqueue()`, the `ingress_history_max_messages` branch is skipped because `own_subnet_type == SubnetType::System`.
5. `state.push_ingress(ingress)` → `canister.push_ingress(msg)` → `CanisterQueues::push_ingress()` → `IngressQueue::push()` — no capacity check, message is enqueued unconditionally.
6. Legitimate user Alice submits a governance proposal. Her message enters the round-robin schedule behind the attacker's backlog and waits an unbounded number of rounds before execution. [9](#0-8) [10](#0-9)

### Citations

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

**File:** rs/replicated_state/src/replicated_state.rs (L1056-1067)
```rust
    pub fn push_ingress(&mut self, msg: Ingress) -> Result<(), IngressInductionError> {
        if msg.is_addressed_to_subnet() {
            self.subnet_queues.push_ingress(msg);
        } else {
            let canister_id = msg.receiver;
            match self.canister_state_make_mut(&canister_id) {
                Some(canister) => canister.push_ingress(msg),
                None => return Err(IngressInductionError::CanisterNotFound(canister_id)),
            }
        }
        Ok(())
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

**File:** rs/ingress_manager/src/ingress_handler.rs (L63-76)
```rust
                // If the ingress pool is full, discard the message.
                // Note: since here we don't remove ingress messages from the ingress pool directly,
                // if `exceeds_limit` returns `true` for a peer `p`, we will remove *all*
                // unvalidated ingress messages originating from that peer. Conversely, we will
                // add all unvalidated ingress message from that peer. This should be okay, as
                // we don't expect to have many unvalidated ingress messages in the pool at any
                // time, because we call `on_state_change` at most every 200ms and every time we
                // receive an ingress message from a peer. Historically, we have had at most 2
                // unvalidated ingress messages in the pool.
                // Since we plan(IC-1718) to have only one section in the Ingress Pool and to
                // validate ingress messages on-the-fly, this problem will eventually go away.
                if pool.exceeds_limit(&ingress_object.originator_id) {
                    return RemoveFromUnvalidated(IngressMessageId::from(ingress_object));
                }
```

**File:** rs/limits/src/lib.rs (L29-37)
```rust
/// The maximum number of messages that can be present in the ingress history
/// at any one time.
///
/// The value is the product of the default `max_ingress_messages_per_block`
/// configured in the subnet record; and the `MAX_INGRESS_TTL` (assuming a block
/// rate of 1 block per second). Times 2, since we could theoretically have
/// `MAX_INGRESS_TTL` worth of `Received` messages; plus the same number of
/// messages in terminal states.
pub const INGRESS_HISTORY_MAX_MESSAGES: usize = 2 * 1000 * MAX_INGRESS_TTL.as_secs() as usize;
```

**File:** rs/limits/src/lib.rs (L78-78)
```rust
pub const MAX_INGRESS_MESSAGES_PER_BLOCK: u64 = 1000;
```

**File:** rs/replicated_state/src/canister_state/queues.rs (L636-639)
```rust
    /// Pushes an ingress message into the induction pool.
    pub fn push_ingress(&mut self, msg: Ingress) {
        self.ingress_queue.push(msg)
    }
```

**File:** rs/replicated_state/src/canister_state/system_state.rs (L1443-1446)
```rust
    /// Pushes an ingress message into the induction pool.
    pub(crate) fn push_ingress(&mut self, msg: Ingress) {
        self.queues.push_ingress(msg)
    }
```
