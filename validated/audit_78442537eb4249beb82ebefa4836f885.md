### Title
Ingress Pool Admission Checks Canister Cycles Per-Message, Not Cumulatively, Enabling Ingress Pool Exhaustion DoS - (File: rs/execution_environment/src/execution_environment.rs)

### Summary

The `should_accept_ingress_message` function performs a per-message, non-cumulative cycles balance check when admitting ingress messages to the ingress pool. An unprivileged attacker can submit many ingress messages targeting a canister that has just enough cycles to pay for a single message. Each message individually passes the balance check and is admitted to the pool. The pool fills with messages that cannot all be executed, throttling legitimate users with HTTP 503 responses.

### Finding Description

The `should_accept_ingress_message` function in `rs/execution_environment/src/execution_environment.rs` is the gate that runs when a user submits an ingress message via the HTTP endpoint. Its own code comment acknowledges it is only a "first-pass check": [1](#0-0) 

The check calls `can_withdraw_cycles_with_threshold` with only `cost` — the cost of the **single incoming message** — against the canister's current balance. It does not consult the ingress pool to account for the cumulative cost of messages already admitted for the same canister: [2](#0-1) 

The "more rigorous check" referenced in the comment is `validate_ingress` inside the ingress selector, which runs only at **block-building time**. That function correctly maintains a `cycles_needed: BTreeMap<CanisterId, Cycles>` accumulator and checks `*cumulative_ingress_cost + ingress_cost` before including a message in a block: [3](#0-2) 

The gap between these two checks is the vulnerability. Messages admitted by the per-message check but rejected by the cumulative check accumulate in the pool and are never included in any block, yet they occupy pool capacity until they expire (`MAX_INGRESS_TTL`).

The ingress pool throttler (`exceeds_threshold`) is per-originating-node and is checked before the ingress filter: [4](#0-3) [5](#0-4) 

Once the local node's quota is exhausted, all subsequent ingress submissions — including legitimate ones — are rejected with HTTP 503.

The `should_accept_ingress_message` check uses the **latest certified state** (updated once per consensus round, ~1 second), so all messages submitted within a single round see the same stale balance and all pass: [6](#0-5) 

### Impact Explanation

An attacker can exhaust the ingress pool of a targeted replica node, causing it to return HTTP 503 to all subsequent ingress submissions for up to `MAX_INGRESS_TTL` (~5 minutes). This is a **cycles/resource accounting bug** that enables a targeted availability DoS against individual replica nodes. Because ingress messages are gossiped between nodes, a pool flooded on one node propagates to peers, potentially amplifying the impact across the subnet.

### Likelihood Explanation

The attack requires no privileged access. Any user can submit ingress messages via the public HTTP endpoint. The attacker only needs to identify a canister whose balance is just above the single-message induction cost (a publicly observable quantity via `canister_status`), then submit a burst of messages within one consensus round. The ingress pool size limits default to `usize::MAX` in the TOML config: [7](#0-6) 

meaning the pool can absorb an arbitrarily large burst before throttling, making the attack easier to execute.

### Recommendation

The `should_accept_ingress_message` function should query the current ingress pool to compute the cumulative induction cost already pending for the paying canister, and check `pending_cost + new_cost` against the canister's balance — mirroring the logic already present in `validate_ingress`. Alternatively, the per-message check should apply a conservative headroom multiplier, or the ingress pool should enforce a per-canister message count/cost cap at admission time.

### Proof of Concept

1. Identify canister `C` with balance `B` where `B ≥ cost_per_message` but `B < N * cost_per_message` for some `N > 1`.
2. Within a single consensus round (~1 second), submit `N` distinct ingress messages to `C` via the HTTP `/api/v2/canister/{id}/call` endpoint.
3. Each message is evaluated by `should_accept_ingress_message` against the same certified state snapshot; each sees `B ≥ cost_per_message` and passes.
4. All `N` messages are admitted to the ingress pool and gossiped.
5. At block-building time, `validate_ingress` admits only the first message (cumulative cost exceeds `B` for messages 2..N); the remaining `N-1` messages sit in the pool until expiry.
6. If `N` exceeds `ingress_pool_max_count` for the local node, `exceeds_threshold()` returns `true` and all subsequent ingress submissions to that node receive HTTP 503 until the pool drains.

### Citations

**File:** rs/execution_environment/src/execution_environment.rs (L3340-3373)
```rust
        // A first-pass check on the canister's balance to prevent needless gossiping
        // if the canister's balance is too low. A more rigorous check happens later
        // in the ingress selector.
        {
            let subnet_cycles_config = state.get_own_subnet_cycles_config();
            let induction_cost = self.cycles_account_manager.ingress_induction_cost(
                ingress,
                effective_canister_id,
                subnet_cycles_config,
            );

            if let IngressInductionCost::Fee { payer, cost } = induction_cost {
                let paying_canister = canister(payer)?;
                let reveal_top_up = paying_canister
                    .controllers()
                    .contains(&ingress.sender().get());
                if let Err(err) = self
                    .cycles_account_manager
                    .can_withdraw_cycles_with_threshold(
                        &paying_canister.system_state,
                        cost,
                        paying_canister.memory_usage(),
                        paying_canister.message_memory_usage(),
                        paying_canister.system_state.reserved_balance(),
                        subnet_cycles_config,
                        reveal_top_up,
                    )
                {
                    return Err(UserError::new(
                        ErrorCode::CanisterOutOfCycles,
                        err.to_string(),
                    ));
                }
            }
```

**File:** rs/ingress_manager/src/ingress_selector.rs (L566-584)
```rust
                    let cumulative_ingress_cost =
                        cycles_needed.entry(payer).or_insert_with(Cycles::zero);
                    if let Err(err) = self
                        .cycles_account_manager
                        .can_withdraw_cycles_with_threshold(
                            &canister.system_state,
                            *cumulative_ingress_cost + ingress_cost,
                            canister.memory_usage(),
                            canister.message_memory_usage(),
                            canister.system_state.reserved_balance(),
                            subnet_cycles_config,
                            false, // error here is not returned back to the user => no need to reveal top up balance
                        )
                    {
                        return Err(ValidationError::InvalidArtifact(
                            InvalidIngressPayloadReason::InsufficientCycles(err),
                        ));
                    }
                    *cumulative_ingress_cost += ingress_cost;
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

**File:** rs/execution_environment/src/ingress_filter.rs (L60-68)
```rust
                let result = match state_reader.get_latest_certified_state() {
                    Some(state) => {
                        let v = exec_env.should_accept_ingress_message(
                            state.take(),
                            &provisional_whitelist,
                            &raw_ingress,
                            ExecutionMode::NonReplicated,
                            &metrics,
                        );
```

**File:** rs/config/src/artifact_pool.rs (L38-39)
```rust
            ingress_pool_max_count: usize::MAX,
            ingress_pool_max_bytes: usize::MAX,
```
