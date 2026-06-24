### Title
Ingress Induction Cycles Charged Before Canister Lifecycle State Check, Causing Unrefunded Cycle Loss on Stopped/Stopping Canisters - (File: rs/messaging/src/scheduling/valid_set_rule.rs)

### Summary
In `ValidSetRuleImpl::enqueue`, ingress induction cycles are deducted from the paying canister's balance **before** the canister's lifecycle status (`Running`/`Stopping`/`Stopped`) is checked. When the canister is in a `Stopping` or `Stopped` state, the function returns an error and the message is rejected — but the already-charged cycles are never refunded. An unprivileged sender can exploit the race window between the ingress filter check and actual induction to cause a target canister to lose cycles without receiving any message.

### Finding Description
In `rs/messaging/src/scheduling/valid_set_rule.rs`, the `enqueue` function handles ingress induction for fee-bearing messages. The ordering of operations is:

1. **Charge cycles** via `charge_ingress_induction_cost` (mutates canister balance).
2. **Check canister status** — if `Stopping` or `Stopped`, return `Err(IngressInductionError::CanisterStopping/Stopped)`. [1](#0-0) 

The cycles deducted in step 1 are **not refunded** in the error path of step 2. The guard that should prevent the charge — the lifecycle status check — comes too late.

The ingress filter (`should_accept_ingress_message`) does check canister status before gossiping/accepting the message: [2](#0-1) 

However, the filter check and the actual induction (`enqueue`) execute in **different protocol phases** (filter at ingress receipt time, induction at block execution time). A `stop_canister` management call processed between these two phases transitions the canister to `Stopping`/`Stopped`, opening the window where `enqueue` is reached with a non-running canister and charges cycles before discovering the state mismatch.

The ingress selector also checks status before cycles: [3](#0-2) 

But `enqueue` in `valid_set_rule.rs` does the opposite — cycles first, status second.

### Impact Explanation
Any cycles charged for a rejected ingress message to a stopped/stopping canister are permanently lost. A malicious or careless actor who sends ingress messages to a canister during its stopping window causes the canister to bleed cycles with no corresponding service rendered. Repeated exploitation can deplete a canister's balance, preventing it from being restarted or paying for future operations. This is a **cycles/resource accounting bug** with a direct financial impact on canister owners.

### Likelihood Explanation
The race window exists between ingress gossip/filter acceptance and block-level induction. On a busy subnet, `stop_canister` calls are processed in the same or adjacent rounds to ingress induction. An attacker who monitors canister state (via `canister_status` queries) and floods ingress messages timed around a `stop_canister` event can reliably hit this window. The attack requires no privileged access — only the ability to send ingress messages (any unprivileged user) and observe public canister status.

### Recommendation
Move the canister lifecycle status check **before** the `charge_ingress_induction_cost` call inside the `IngressInductionCost::Fee` branch of `enqueue`. Specifically, check `canister.status()` immediately after obtaining the mutable canister reference and return `Err(IngressInductionError::CanisterStopping/Stopped)` before any cycles are deducted. This mirrors the correct ordering already used in the ingress selector: [3](#0-2) 

### Proof of Concept

**Root cause location:** `rs/messaging/src/scheduling/valid_set_rule.rs`, `enqueue` function, `IngressInductionCost::Fee` branch. [1](#0-0) 

**Trigger sequence:**
1. Canister C is `Running` on an application subnet.
2. Attacker A submits ingress message M to C. The ingress filter accepts M (canister is running).
3. Before M is inducted in the next round, a `stop_canister` call for C is processed, transitioning C to `Stopping`.
4. Message routing calls `induct_message` → `enqueue` for M.
5. `enqueue` enters the `Fee` branch, calls `charge_ingress_induction_cost` — cycles are deducted from C's balance.
6. `enqueue` then checks `canister.status()` → `Stopping` → returns `Err(CanisterStopping)`.
7. M is not inducted; cycles are not refunded.

**Existing tests confirm the failure path exists** (message is rejected for stopped/stopping canisters) but do not assert that cycles are preserved: [4](#0-3) 

The absence of a cycle-balance assertion in these tests is itself evidence that the unrefunded-cycles case is untested and unguarded.

### Citations

**File:** rs/messaging/src/scheduling/valid_set_rule.rs (L293-335)
```rust
            IngressInductionCost::Fee { payer, cost } => {
                // Get the paying canister from the state.
                let canister = match state.canister_state_make_mut(&payer) {
                    Some(canister) => canister,
                    None => return Err(IngressInductionError::CanisterNotFound(payer)),
                };

                // Withdraw cost of inducting the message.
                let memory_usage = canister.memory_usage();
                let message_memory_usage = canister.message_memory_usage();
                let compute_allocation = canister.compute_allocation();
                let reveal_top_up = canister.controllers().contains(&ingress.source.get());
                if let Err(err) = self.cycles_account_manager.charge_ingress_induction_cost(
                    canister,
                    memory_usage,
                    message_memory_usage,
                    compute_allocation,
                    cost,
                    subnet_cycles_config,
                    reveal_top_up,
                ) {
                    return Err(IngressInductionError::CanisterOutOfCycles(err));
                }

                // Ensure the canister is running if the message isn't to a subnet.
                if !ingress.is_addressed_to_subnet() {
                    match canister.status() {
                        CanisterStatusType::Running => {}
                        CanisterStatusType::Stopping => {
                            return Err(IngressInductionError::CanisterStopping(
                                canister.canister_id(),
                            ));
                        }
                        CanisterStatusType::Stopped => {
                            return Err(IngressInductionError::CanisterStopped(
                                canister.canister_id(),
                            ));
                        }
                    }
                }

                state.push_ingress(ingress)
            }
```

**File:** rs/execution_environment/src/execution_environment.rs (L3387-3401)
```rust
        match canister_state.status() {
            CanisterStatusType::Running => {}
            CanisterStatusType::Stopping => {
                return Err(UserError::new(
                    ErrorCode::CanisterStopping,
                    format!("Canister {} is stopping", ingress.canister_id()),
                ));
            }
            CanisterStatusType::Stopped => {
                return Err(UserError::new(
                    ErrorCode::CanisterStopped,
                    format!("Canister {} is stopped", ingress.canister_id()),
                ));
            }
        }
```

**File:** rs/ingress_manager/src/ingress_selector.rs (L536-548)
```rust
            match canister_state.status() {
                CanisterStatusType::Running => {}
                CanisterStatusType::Stopping => {
                    return Err(ValidationError::InvalidArtifact(
                        InvalidIngressPayloadReason::CanisterStopping(canister_id),
                    ));
                }
                CanisterStatusType::Stopped => {
                    return Err(ValidationError::InvalidArtifact(
                        InvalidIngressPayloadReason::CanisterStopped(canister_id),
                    ));
                }
            }
```

**File:** rs/messaging/src/scheduling/valid_set_rule/test.rs (L154-209)
```rust
#[test]
fn induct_message_fails_for_stopping_canister() {
    with_test_replica_logger(|log| {
        let canister_id = canister_test_id(0);
        let signed_ingress = SignedIngressBuilder::new()
            .canister_id(canister_id)
            .sender(user_test_id(2))
            .build();
        let msg = signed_ingress.content();
        let msg_id = msg.id();
        let mut ingress_history_writer = MockIngressHistory::new();
        ingress_history_writer
            .expect_set_status()
            .with(
                always(),
                eq(msg.id()),
                eq(IngressStatus::Known {
                    receiver: canister_id.get(),
                    user_id: user_test_id(2),
                    time: UNIX_EPOCH,
                    state: IngressState::Failed(UserError::new(
                        ErrorCode::CanisterStopping,
                        format!("Canister {canister_id} is stopping"),
                    )),
                }),
                always(),
            )
            .times(1)
            .returning(move |state, _, status, _| {
                state.set_ingress_status(msg_id.clone(), status, NumBytes::from(u64::MAX), |_| {});
                IngressStatus::Unknown.into()
            });
        let ingress_history_writer = Arc::new(ingress_history_writer);
        let metrics_registry = MetricsRegistry::new();
        let valid_set_rule = ValidSetRuleImpl::new(
            ingress_history_writer,
            Arc::new(CyclesAccountManagerBuilder::new().build()),
            &metrics_registry,
            log,
        );

        let mut state = ReplicatedState::new(subnet_test_id(1), SubnetType::Application);
        state.put_canister_state(get_stopping_canister(canister_id));

        valid_set_rule.induct_message(&mut state, signed_ingress, ExecutionRound::from(0));
        assert_eq!(ingress_queue_size(&state, canister_id), 0);
        assert_inducted_ingress_messages_eq(
            metric_vec(&[(&[(LABEL_STATUS, LABEL_VALUE_CANISTER_STOPPING)], 1)]),
            &metrics_registry,
        );
        assert_eq!(
            0,
            fetch_inducted_payload_size_stats(&metrics_registry).count
        );
    });
}
```
