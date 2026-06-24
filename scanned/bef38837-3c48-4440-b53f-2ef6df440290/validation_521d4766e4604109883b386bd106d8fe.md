### Title
Ingress Pool Admission DoS via Per-Message vs. Cumulative Cycles Check Mismatch — (`rs/execution_environment/src/execution_environment.rs`, `rs/ingress_manager/src/ingress_selector.rs`)

---

### Summary

The IC ingress admission gate (`should_accept_ingress_message`) checks whether a canister can afford a **single** message's induction cost. The proposal-time gate (`validate_ingress`) checks whether the canister can afford the **cumulative** cost of all messages from that canister selected for the current block. This structural mismatch allows an unprivileged attacker to fill the validated ingress pool with messages that pass admission but fail at proposal time, where the failing messages are never evicted — they remain in the pool until TTL expiry. The pool can be saturated, blocking legitimate ingress admission.

---

### Finding Description

**Admission-time check** (`should_accept_ingress_message`, `rs/execution_environment/src/execution_environment.rs`):

The code explicitly labels this a "first-pass" check and checks only the cost of the single message being submitted:

```rust
// A first-pass check on the canister's balance to prevent needless gossiping
// if the canister's balance is too low. A more rigorous check happens later
// in the ingress selector.
if let IngressInductionCost::Fee { payer, cost } = induction_cost {
    ...
    if let Err(err) = self.cycles_account_manager.can_withdraw_cycles_with_threshold(
        &paying_canister.system_state,
        cost,   // ← single-message cost only, non-cumulative
        ...
    ) { return Err(...); }
}
``` [1](#0-0) 

**Pool validation** (`validate_ingress_pool_object`, `rs/ingress_manager/src/ingress_handler.rs`):

When messages move from unvalidated to validated, **no cycles check is performed at all** — only size, known-status, and signature are checked:

```rust
fn validate_ingress_pool_object(...) -> Result<(), IngressMessageValidationError> {
    // size check
    // already-known check
    // signature check
    Ok(())  // ← no cycles check
}
``` [2](#0-1) 

**Proposal-time check** (`validate_ingress`, `rs/ingress_manager/src/ingress_selector.rs`):

At block-building time, the check is **cumulative** across all messages from the same canister in the current block:

```rust
let cumulative_ingress_cost = cycles_needed.entry(payer).or_insert_with(Cycles::zero);
if let Err(err) = self.cycles_account_manager.can_withdraw_cycles_with_threshold(
    &canister.system_state,
    *cumulative_ingress_cost + ingress_cost,  // ← cumulative cost
    ...
) {
    return Err(ValidationError::InvalidArtifact(
        InvalidIngressPayloadReason::InsufficientCycles(err),
    ));
}
*cumulative_ingress_cost += ingress_cost;
``` [3](#0-2) 

**No eviction of failing messages**: When `validate_ingress` fails during `get_ingress_payload`, the message is only popped from the **local in-memory queue** for that round — it is not removed from the validated pool:

```rust
_ => {
    queue.msgs.pop();   // ← local queue only, not the pool
    continue;
}
``` [4](#0-3) 

The validated pool is only cleaned up by expiry (`PurgeBelowExpiry`), finalization, or `IngressHistoryReader` returning a non-Unknown status — none of which apply to messages that fail cycles validation: [5](#0-4) 

The pool capacity is enforced by `ingress_pool_max_count` and `ingress_pool_max_bytes` per peer: [6](#0-5) 

---

### Impact Explanation

An attacker who controls a canister with cycles sufficient for exactly **one** message can submit **N** messages via the HTTP endpoint. Each message passes the per-message admission check independently (the check sees the same current state for each submission). All N messages enter the validated pool. At proposal time, only the first message passes the cumulative check; messages 2 through N fail with `InsufficientCycles` and remain in the validated pool until `MAX_INGRESS_TTL` (5 minutes) expires.

By using multiple canisters (each funded for one message) and submitting from multiple boundary nodes, the attacker can saturate the validated pool across all subnet nodes. Once the pool is full, the `exceeds_threshold` check at the HTTP endpoint rejects all new legitimate ingress submissions with `SERVICE_UNAVAILABLE`: [7](#0-6) 

The result is:
- **Ingress admission DoS**: legitimate users cannot submit messages for up to 5 minutes per attack wave.
- **Wasted proposal work**: the ingress selector iterates over zombie messages every round, discarding them locally but never evicting them from the pool.

---

### Likelihood Explanation

The attack is reachable by any unprivileged user with a canister holding a minimal cycles balance (enough for one induction cost, which is proportional to message size — a few hundred bytes costs a negligible amount). No privileged access, governance majority, or threshold corruption is required. The attacker submits messages via the standard HTTPS call endpoint. The attack can be repeated every 5 minutes to maintain the DoS. The structural mismatch is present in production code and is not gated by any feature flag.

---

### Recommendation

1. **Tighten admission-time gate**: Track per-canister cumulative ingress cost at admission time (or at minimum, check whether the canister's balance exceeds the freeze threshold by more than a single message cost before admitting multiple messages from the same canister).

2. **Evict messages that fail cycles validation**: When `validate_ingress` returns `InsufficientCycles` during `get_ingress_payload`, emit a `RemoveFromValidated` change action so the message is purged from the pool rather than re-selected every round.

3. **Per-canister admission rate limiting**: Limit the number of messages from a single canister that can be admitted to the pool within a TTL window, proportional to the canister's available cycles.

---

### Proof of Concept

1. Create canister `C` with cycles balance = `freeze_threshold + ingress_induction_cost(msg)` (enough for exactly one message).
2. Submit `N` identical messages targeting `C` via the HTTPS `/api/v2/canister/{id}/call` endpoint in rapid succession. Each call invokes `should_accept_ingress_message`, which checks `can_withdraw_cycles_with_threshold(cost_of_one_message)` against the same current state — all N pass.
3. All N messages enter the unvalidated pool and are moved to validated by `validate_ingress_pool_object` (no cycles check).
4. On the next `get_ingress_payload` call, `validate_ingress` checks cumulative cost: message 1 passes (`cumulative = cost`), message 2 fails (`cumulative = 2×cost > balance - threshold`), messages 3–N fail similarly. Messages 2–N are popped from the local queue but remain in the validated pool.
5. Repeat with additional canisters until `ingress_pool.exceeds_threshold()` returns `true`.
6. Legitimate users now receive `503 Service Unavailable` for all ingress submissions until the attacker's messages expire after `MAX_INGRESS_TTL`.

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

**File:** rs/ingress_manager/src/ingress_handler.rs (L112-137)
```rust
        // Check validated messages and remove if they are not required anymore (i.e.
        // IngressHistoryReader returns status other than Unknown).
        for validated_artifact in pool.validated().get_all_by_expiry_range(expiry_range) {
            let ingress_object = &validated_artifact.msg;

            // Check status of the ingress message against IngressHistoryReader,
            // If Unknown, consider the ingress message valid
            let status = get_status(&ingress_object.message_id);
            if status != IngressStatus::Unknown {
                debug!(
                    self.log,
                    "ingress_message_remove_validated";
                    ingress_message.message_id => format!("{}", ingress_object.message_id),
                    ingress_message.reason => format!("{:?}", status),
                );
                change_set.push(RemoveFromValidated(IngressMessageId::from(ingress_object)));
            }
        }

        // Also include finalized messages that were requested to purge.
        let mut to_purge = self.messages_to_purge.write().unwrap();
        while let Some(message_ids) = to_purge.pop() {
            for id in message_ids {
                change_set.push(RemoveFromValidated(id));
            }
        }
```

**File:** rs/ingress_manager/src/ingress_handler.rs (L167-206)
```rust
    fn validate_ingress_pool_object(
        &self,
        ingress_object: &IngressPoolObject,
        settings: &IngressMessageSettings,
        ingress_message_status: impl Fn(&MessageId) -> IngressStatus,
        consensus_time: Time,
        registry_version: RegistryVersion,
    ) -> Result<(), IngressMessageValidationError> {
        // If the message is too large, consider the ingress message invalid
        let size = ingress_object.count_bytes();
        if size > settings.max_ingress_bytes_per_message {
            return Err(IngressMessageValidationError::IngressMessageTooLarge {
                max: settings.max_ingress_bytes_per_message,
                actual: size,
            });
        }

        match ingress_message_status(&ingress_object.message_id) {
            IngressStatus::Known { .. } => {
                return Err(IngressMessageValidationError::IngressMessageAlreadyKnown);
            }
            IngressStatus::Unknown => {}
        }

        // Check signatures, remove from unvalidated if they can't be
        // verified, add to validated otherwise.
        //
        // Note that consensus_time is used here instead of current_time,
        // in order to be consistent with expiry_range, which imposes
        // a precondition that all messages processed here are in range.
        if let Err(err) = self.request_validator.validate_request(
            ingress_object.signed_ingress.as_ref(),
            consensus_time,
            &self.registry_root_of_trust_provider(registry_version),
        ) {
            return Err(IngressMessageValidationError::InvalidRequest(err));
        }

        Ok(())
    }
```

**File:** rs/ingress_manager/src/ingress_selector.rs (L204-207)
```rust
                        _ => {
                            queue.msgs.pop();
                            continue;
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
