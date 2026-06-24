### Title
Ingress Pool Exhaustion via No Cross-Sender Eviction and No Per-Principal Slot Limit — (`rs/artifact_pool/src/ingress_pool.rs`)

### Summary

The IC ingress pool (`IngressPoolImpl`) enforces a bounded capacity per replica node (production: 10,000 messages / 100 MB). The throttle gate at the HTTP endpoint (`exceeds_threshold()`) checks only the local node's own aggregate message count, with no per-sender (principal) sub-limit and no cross-sender eviction strategy. A single unprivileged ingress sender can fill the entire pool by submitting messages up to the node-level cap, causing all other users routed to that node to receive `503 Service Unavailable` for the duration of the attack.

### Finding Description

The ingress pool is implemented in `rs/artifact_pool/src/ingress_pool.rs`. It holds two sections — `validated` and `unvalidated` — and tracks per-peer (NodeId) byte and message counters via `PeerCounters`. [1](#0-0) 

The throttle interface used by the HTTP endpoint is: [2](#0-1) 

`exceeds_threshold()` calls `exceeds_limit(&self.node_id)`, which sums the byte and message counters for the **local node only**: [3](#0-2) 

Every ingress message submitted through the public HTTP endpoint is tagged with `peer_id = node_id` (the local replica's own NodeId). This means all HTTP-submitted messages — regardless of which principal signed them — are counted against the same single per-node bucket. There is no sub-limit per sender principal.

The HTTP call handler checks this throttle before accepting any new message: [4](#0-3) 

When `exceeds_threshold()` returns `true`, the endpoint returns `503` and drops the request. There is no eviction of lower-priority or older messages from other senders to make room. The ingress handler in `rs/ingress_manager/src/ingress_handler.rs` also only drops messages when a **specific peer** exceeds its limit, not when a specific principal does: [5](#0-4) 

The production node configuration sets the pool limits to: [6](#0-5) 

`ingress_pool_max_count: 10000` and `ingress_pool_max_bytes: 100000000`. These are the only guards. The default in `ArtifactPoolTomlConfig::new()` is `usize::MAX` (effectively unlimited), so the production template is the binding constraint: [7](#0-6) 

### Impact Explanation

An unprivileged ingress sender submits exactly 10,000 signed ingress messages (each with a distinct nonce and an expiry time set to `MAX_INGRESS_TTL` in the future) targeting any canister that does not reject messages via `canister_inspect_message`. Once the pool is full, `exceeds_threshold()` returns `true` and every subsequent HTTP call to that replica node is rejected with `503 Service Unavailable`. Legitimate users routed to that node cannot submit update calls for the duration of the attack. The attacker refreshes expiring messages to sustain the condition indefinitely. No cross-sender eviction exists to displace the attacker's messages in favour of legitimate traffic.

### Likelihood Explanation

The attack requires submitting 10,000 cryptographically signed messages. Ingress messages carry no direct fee to the sender (execution cost is charged to the target canister). The attacker needs a valid identity and a target canister that accepts messages. Both are trivially obtainable on mainnet. The 10,000-message cap is a fixed, publicly documented production value. The attack is therefore low-cost and repeatable.

### Recommendation

1. **Introduce a per-sender (principal) sub-limit** inside `IngressPoolImpl`. Track message count and byte usage per sender principal in addition to per-peer node, and reject or evict messages from a single principal that exceeds a fraction of the total pool capacity.
2. **Implement a cross-sender eviction strategy**: when the pool is full and a new message arrives, evict the oldest message from the principal with the highest current message count rather than rejecting the incoming message outright.
3. **Enforce the per-principal limit at the HTTP endpoint** before the pool-level throttle, so a single principal cannot monopolise the pool regardless of which node they target.

### Proof of Concept

```
# Attacker controls principal P and targets canister C (no canister_inspect_message).
# Node N has ingress_pool_max_count = 10000.

for i in 1..=10000:
    submit_ingress(
        sender    = P,
        canister  = C,
        method    = "update",
        nonce     = i,
        expiry    = now() + MAX_INGRESS_TTL,   # up to 5 minutes
    )

# Pool on node N is now full.
# exceeds_threshold() → true (self.node_id counter ≥ ingress_pool_max_count)

# Any subsequent call from a legitimate user U:
submit_ingress(sender=U, canister=C2, ...) → HTTP 503 "Service is overloaded"

# Attacker refreshes every ~5 minutes to maintain the condition.
```

The root cause is in `rs/artifact_pool/src/ingress_pool.rs` at `exceeds_threshold()` (line 363) and `exceeds_limit()` (line 226), which aggregate all HTTP-submitted messages into a single node-level bucket with no per-principal sub-accounting and no eviction path. [3](#0-2) [2](#0-1) [4](#0-3) [6](#0-5)

### Citations

**File:** rs/artifact_pool/src/ingress_pool.rs (L178-186)
```rust
pub struct IngressPoolImpl {
    validated: IngressPoolSection<ValidatedIngressArtifact>,
    unvalidated: IngressPoolSection<UnvalidatedIngressArtifact>,
    metrics: IngressPoolMetrics,
    ingress_pool_max_count: usize,
    ingress_pool_max_bytes: usize,
    node_id: NodeId,
    log: ReplicaLogger,
}
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

**File:** rs/artifact_pool/src/ingress_pool.rs (L362-372)
```rust
impl IngressPoolThrottler for IngressPoolImpl {
    fn exceeds_threshold(&self) -> bool {
        if self.exceeds_limit(&self.node_id) {
            self.metrics.ingress_messages_throttled.inc();

            true
        } else {
            false
        }
    }
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

**File:** rs/ic_os/config/tool/templates/ic.json5.template (L49-50)
```text
        ingress_pool_max_count: 10000,
        ingress_pool_max_bytes: 100000000,
```

**File:** rs/config/src/artifact_pool.rs (L36-43)
```rust
        Self {
            consensus_pool_path,
            ingress_pool_max_count: usize::MAX,
            ingress_pool_max_bytes: usize::MAX,
            consensus_pool_backend: Some("lmdb".to_string()),
            backup,
        }
    }
```
