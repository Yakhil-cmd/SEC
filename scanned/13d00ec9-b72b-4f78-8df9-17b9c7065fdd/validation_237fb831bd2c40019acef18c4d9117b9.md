### Title
Malicious Delegated Node Can Suppress `NonReplicated` Canister HTTP Outcalls, Forcing Timeout - (`rs/https_outcalls/consensus/src/pool_manager.rs`)

---

### Summary

The IC's `NonReplicated` canister HTTP outcall mode delegates the HTTP request to a single randomly-chosen node. That node is the only one that will ever make the request. A malicious node that is selected as the delegate can simply refuse to send the request to the adapter, and no other node can detect or compensate for this omission. The only protocol-level recourse is the 60-second timeout, after which the canister receives a `SysTransient` rejection.

---

### Finding Description

When a canister calls `ic00::HttpRequest` with `is_replicated: Some(false)`, execution creates a `CanisterHttpRequestContext` with `replication: Replication::NonReplicated(delegated_node_id)`, where `delegated_node_id` is a single node chosen uniformly at random from the subnet's node set. [1](#0-0) 

This context is stored in replicated state. Every node's `CanisterHttpPoolManagerImpl::make_new_requests` then checks `is_authorized_signer` to decide whether to forward the request to the local HTTP adapter: [2](#0-1) 

`is_authorized_signer` for `NonReplicated` returns `true` only for the single delegated node: [3](#0-2) 

There is **no enforcement mechanism** that compels the delegated node to call `http_adapter_shim.send(...)`. A malicious node simply skips this call. No other node will ever make the request (they all skip it via the `is_authorized_signer` guard). The only protocol-level fallback is the timeout: [4](#0-3) 

The block maker detects the timeout and includes it in the payload once `request.time + CANISTER_HTTP_TIMEOUT_INTERVAL < validation_context.time`: [5](#0-4) 

The existing code comments acknowledge that malicious nodes can cause timeouts in the fully-replicated case (by signing conflicting responses), but the `NonReplicated` case is structurally worse: a single below-threshold node can unilaterally suppress the request with zero observable evidence. [6](#0-5) 

---

### Impact Explanation

Any canister that issues a `NonReplicated` HTTP outcall (`is_replicated: Some(false)`) is vulnerable. When the randomly-selected delegated node is malicious:

1. The HTTP request is never sent to the external server.
2. No share is ever produced for that `CallbackId`.
3. After 60 seconds the block maker includes a timeout entry.
4. The canister receives `RejectCode::SysTransient` / "Canister http request timed out".

The canister's update call is blocked for the full 60-second window and then fails. For canisters that rely on non-replicated outcalls for correctness (e.g., PUT/DELETE/PATCH operations, which are only permitted in non-replicated mode), this is a targeted denial-of-service that cannot be distinguished from a genuine network failure. [7](#0-6) 

---

### Likelihood Explanation

- The attacker is a **single node below the consensus fault threshold** — the minimum viable adversary on the IC.
- For a 13-node subnet the probability of being selected as delegate is ~7.7% per request; for a 34-node subnet ~2.9%. A canister making many non-replicated outcalls will eventually hit the malicious node.
- The malicious node incurs no penalty: skipping `http_adapter_shim.send(...)` is indistinguishable from a network failure to all other nodes.
- No special privilege, key material, or majority is required.

---

### Recommendation

1. **Re-delegate on timeout**: When a `NonReplicated` request times out, re-issue it to a different randomly-chosen node rather than immediately returning an error to the canister. This limits the impact of a single malicious node to a one-round delay.
2. **Require a signed "I attempted this request" artifact**: The delegated node should be required to produce a signed acknowledgement (even a signed rejection) within a sub-timeout window; absence of any artifact from the delegate within that window is detectable by the block maker and can trigger re-delegation.
3. **Document the trust assumption**: At minimum, the `NonReplicated` API documentation should explicitly state that a single malicious node can suppress the request and force a timeout, so canister developers can make an informed choice.

---

### Proof of Concept

1. Canister C calls `ic00::HttpRequest` with `is_replicated: Some(false)`.
2. Execution stores `Replication::NonReplicated(node_X)` in replicated state (node X chosen at random).
3. Node X is malicious. In its `make_new_requests` loop it sees `is_authorized_signer` returns `true` for itself, but it simply does **not** call `http_adapter_shim.send(...)` (or patches its binary to skip the send).
4. All other nodes see `is_authorized_signer` return `false` and skip the request.
5. No `CanisterHttpResponseShare` is ever produced for this `CallbackId`.
6. After `CANISTER_HTTP_TIMEOUT_INTERVAL` (60 s) the block maker includes the `CallbackId` in `payload.timeouts`.
7. Execution delivers `RejectCode::SysTransient` / "Canister http request timed out" to canister C. [8](#0-7) [9](#0-8)

### Citations

**File:** rs/types/types/src/canister_http.rs (L36-43)
```rust
//! Early detection of non-deterministic server responses is not guaranteed to work if malicious nodes are present,
//! which sign multiple different responses for the same request.
//! In that case, the non-determisitic server responses will time out using the timeout mechanism (see 4c).
//!
//! 4c. If neither 4a nor 4b yield a result after a certrain amount of time, the timeout mechanism ends the request.
//! The blockmaker indicates, which requests have timed out, i.e. the blocktime of the latest finalized block is higher than
//! the timestamp of a request plus the timeout interval. This condition is verifiable by the other nodes in the network.
//! Once a timeout has made it into a finalized block, the request is answered with an error message.
```

**File:** rs/types/types/src/canister_http.rs (L78-79)
```rust
/// Time after which a response is considered timed out and a timeout error will be returned to execution
pub const CANISTER_HTTP_TIMEOUT_INTERVAL: Duration = Duration::from_secs(60);
```

**File:** rs/types/types/src/canister_http.rs (L169-193)
```rust
#[derive(Clone, Eq, PartialEq, Hash, Debug, Deserialize, Serialize)]
pub enum Replication {
    /// The request is fully replicated, i.e. all nodes will attempt the http request.
    FullyReplicated,
    /// The request is not replicated, i.e. only the node with the given `NodeId` will attempt the http request.
    NonReplicated(NodeId),
    /// The request is sent to a committee of nodes that all attempt the http request.
    /// The canister receives between `min_responses` and `max_responses` (potentially differing) responses.
    Flexible {
        committee: BTreeSet<NodeId>,
        min_responses: u32,
        max_responses: u32,
    },
}

impl Replication {
    /// Returns true if the given node is authorized to sign a share, assuming
    /// it is part of the canister HTTP committee.
    pub fn is_authorized_signer(&self, signer: &NodeId) -> bool {
        match self {
            Replication::FullyReplicated => true,
            Replication::NonReplicated(node_id) => node_id == signer,
            Replication::Flexible { committee, .. } => committee.contains(signer),
        }
    }
```

**File:** rs/types/types/src/canister_http.rs (L541-556)
```rust
        // Allow PUT, DELETE, and PATCH only in non-replicated mode to avoid
        // confusing race conditions that may occur.
        // For example, if first a DELETE outcall for resource R is made,
        // directly followed by a PUT, PATCH, or POST outcall for R, in
        // replicated mode it may happen that R is actually deleted after the
        // PUT/PATCH/POST outcall has finished, because the IC does not
        // necessarily wait for all outcalls to complete before a result is
        // delivered back to the canister: The IC only waits for sufficient
        // calls to complete to reach consensus on the result.
        if matches!(
            args.method,
            HttpMethod::PUT | HttpMethod::DELETE | HttpMethod::PATCH
        ) && args.is_replicated != Some(false)
        {
            return Err(CanisterHttpRequestContextError::DeterministicResponseCountRequired);
        }
```

**File:** rs/types/types/src/canister_http.rs (L558-569)
```rust
        let replication = match args.is_replicated {
            Some(false) => {
                let delegated_node_id = node_ids
                    .iter()
                    .copied()
                    .choose(rng)
                    .ok_or(CanisterHttpRequestContextError::NoNodesAvailableForDelegation)?;

                Replication::NonReplicated(delegated_node_id)
            }
            _ => Replication::FullyReplicated,
        };
```

**File:** rs/https_outcalls/consensus/src/pool_manager.rs (L251-315)
```rust
    /// Inform the HttpAdapterShim of any new requests that must be made.
    fn make_new_requests(&self, canister_http_pool: &dyn CanisterHttpPool) {
        let _time = self
            .metrics
            .op_duration
            .with_label_values(&["make_new_requests"])
            .start_timer();

        let http_requests = &self
            .latest_state()
            .metadata
            .subnet_call_context_manager
            .canister_http_request_contexts;

        self.metrics
            .in_flight_requests
            .set(http_requests.len().try_into().unwrap());

        let request_ids_in_pool: BTreeSet<_> = canister_http_pool
            .get_validated_shares()
            .filter_map(|share| {
                if share.signature.signer == self.replica_config.node_id {
                    Some(share.content.id())
                } else {
                    None
                }
            })
            .collect();

        let request_ids_already_made: BTreeSet<_> = request_ids_in_pool
            .union(&self.requested_id_cache.borrow())
            .cloned()
            .collect();

        let socks_proxy_addrs = self.get_socks_proxy_addrs();

        for (id, context) in http_requests {
            if !context
                .replication
                .is_authorized_signer(&self.replica_config.node_id)
            {
                continue;
            }

            if !request_ids_already_made.contains(id) {
                if let Err(err) = self
                    .http_adapter_shim
                    .lock()
                    .unwrap()
                    .send(CanisterHttpRequest {
                        id: *id,
                        context: context.clone(),
                        socks_proxy_addrs: socks_proxy_addrs.clone(),
                    })
                {
                    warn!(
                        self.log,
                        "Failed to add canister http request to queue {:?}", err
                    )
                } else {
                    self.requested_id_cache.borrow_mut().insert(*id);
                }
            }
        }
    }
```

**File:** rs/https_outcalls/consensus/src/payload_builder.rs (L234-256)
```rust
                if request.time + CANISTER_HTTP_TIMEOUT_INTERVAL < validation_context.time {
                    // Because timeouts are very cheap to verify, they are
                    // not counted as responses (so that they are irrelevant
                    // for the CANISTER_HTTP_MAX_RESPONSES_PER_BLOCK limit.
                    if matches!(request.replication, Replication::Flexible { .. }) {
                        let error = FlexibleCanisterHttpError::Timeout {
                            callback_id: *callback_id,
                        };
                        let candidate_size = error.count_bytes();
                        let size = NumBytes::new((accumulated_size + candidate_size) as u64);
                        if size < max_payload_size {
                            flexible_errors.push(error);
                            accumulated_size += candidate_size;
                        }
                    } else {
                        let candidate_size = callback_id.count_bytes();
                        let size = NumBytes::new((accumulated_size + candidate_size) as u64);
                        if size < max_payload_size {
                            timeouts.push(*callback_id);
                            accumulated_size += candidate_size;
                        }
                    }
                    continue;
```
