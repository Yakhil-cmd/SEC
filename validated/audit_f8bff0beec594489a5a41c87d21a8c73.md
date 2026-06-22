### Title
SNS Governance `perform_execute_generic_nervous_system_function` Lacks Re-entrancy Protection During Inter-Canister Proposal Execution - (File: `rs/sns/governance/src/canister_control.rs`)

---

### Summary

The SNS governance canister executes `ExecuteGenericNervousSystemFunction` proposals by making an inter-canister call to an arbitrary, community-registered external canister. While the governance canister is suspended awaiting that call, it remains able to process other incoming messages. A malicious or compromised target canister can exploit this by calling back into governance's public `manage_neuron` update method to cast a deciding vote on a second pending proposal, triggering its execution — including a second call to the same target canister — before the first proposal's execution has completed. This is the IC analog of the Aragon `DAO.execute` re-entrancy: an external callee re-enters the governance execution path and disrupts the intended proposal ordering.

---

### Finding Description

**Root cause — no re-entrancy guard on the inter-canister call:**

`perform_execute_generic_nervous_system_function_call` in `rs/sns/governance/src/canister_control.rs` makes an unauthenticated, unguarded `await` to an arbitrary external canister:

```rust
let result = env
    .call_canister(
        valid_function.target_canister_id,
        &valid_function.target_method,
        call.payload,
    )
    .await;          // ← governance is suspended here; other messages are processed
``` [1](#0-0) 

Its caller, `perform_execute_generic_nervous_system_function`, sets no in-progress flag and acquires no lock before delegating to this function: [2](#0-1) 

**Re-entrancy path through `manage_neuron` → `register_vote` → `process_proposal`:**

While governance is suspended at the `await`, the IC runtime can deliver and fully process any other ingress or inter-canister message to the governance canister. The public `#[update]` endpoint `manage_neuron` is reachable by any canister: [3](#0-2) 

`register_vote` inside `manage_neuron` synchronously calls `process_proposal`: [4](#0-3) 

`process_proposal` — if the voted-on proposal now has quorum — immediately calls `start_proposal_execution`, which spawns a new async task via `spawn_in_canister_env`: [5](#0-4) 

`start_proposal_execution` transmutes `self` to `'static` and spawns the new `perform_action` future, which can itself call `perform_execute_generic_nervous_system_function` and issue another inter-canister call to the same target canister: [6](#0-5) 

**Concrete execution sequence:**

1. Proposal A (`ExecuteGenericNervousSystemFunction(id=1000)`) is adopted; governance calls target canister X and suspends.
2. Canister X, in its execution context, sends a `manage_neuron / RegisterVote` message to governance for proposal B (`ExecuteGenericNervousSystemFunction(id=1000)`), which is still `Open` and one vote short of quorum.
3. Governance processes the `manage_neuron` message: `register_vote` → `process_proposal` → `start_proposal_execution` → spawns `perform_action(B)`.
4. The spawned future runs immediately: governance calls canister X again for proposal B and suspends.
5. Canister X processes proposal B's call and replies; governance resumes, marks proposal B `Executed`.
6. Canister X replies to governance's original call for proposal A; governance resumes, marks proposal A `Executed`.

Proposal B has now executed **inside** proposal A's execution window, with canister X receiving both calls in an order it fully controls.

---

### Impact Explanation

- **Ordering violation**: The SNS community may have intended proposal A to complete before proposal B begins (e.g., A sets up state that B depends on). The re-entrancy inverts this.
- **Double-effect exploitation**: If the same `GenericNervousSystemFunction` is registered for both proposals, the target canister receives two governance-authorized calls in a single round-trip. Any side-effect that the canister performs on each call (token minting, permission grants, state transitions) is doubled.
- **State manipulation mid-execution**: The target canister can observe governance state (e.g., treasury balances, parameter values) between the two calls and craft its second response to exploit the intermediate state.
- **Governance parameter tampering**: If proposal B is a `ManageNervousSystemParameters` action (not requiring an external call), it executes synchronously inside the `manage_neuron` callback, changing voting thresholds or other parameters while proposal A is still in flight.

---

### Likelihood Explanation

The attack requires the target canister to control — or have been delegated voting permission over — a neuron whose vote is the deciding one for proposal B. This is a realistic condition for SNS DAOs where:

- The dapp canister itself holds a developer neuron (common in SNS launches).
- A proposal B is submitted and pre-voted to the threshold, waiting for one final vote.
- The attacker controls both the registered target canister and a neuron eligible to cast that final vote.

No privileged role, no key compromise, and no subnet-majority is required. The attacker-controlled entry path is a standard ingress `manage_neuron` call issued by the target canister during its own execution of proposal A's inter-canister call.

---

### Recommendation

1. **Add a per-function-id execution lock** in `perform_execute_generic_nervous_system_function`. Before the `await`, insert the `function_id` into a `BTreeSet<u64>` stored in governance state; remove it in a `defer`-style cleanup after the `await`. Reject any concurrent `ExecuteGenericNervousSystemFunction` call for the same `function_id` with an `ErrorType::ResourceExhausted` error.

2. **Alternatively, add a global execution-in-progress flag** (similar to the `finalize_swap_in_progress` flag used in the SNS Swap canister) that blocks new proposal executions while any `ExecuteGenericNervousSystemFunction` is awaiting a reply.

3. **At minimum, document** in the `NervousSystemFunction` registration interface that target canisters must not call back into governance's `manage_neuron` during execution, and that SNS DAOs should not register canisters that hold voting neurons as `GenericNervousSystemFunction` targets.

---

### Proof of Concept

**Setup:**
- SNS has `NervousSystemFunction` id=1000 targeting canister X (`target_canister_id = X`, `target_method = "execute"`).
- Canister X controls neuron N, which has enough voting power to be the deciding vote on any proposal.
- Proposal A: `ExecuteGenericNervousSystemFunction { function_id: 1000, payload: b"action_a" }` — adopted, awaiting execution.
- Proposal B: `ExecuteGenericNervousSystemFunction { function_id: 1000, payload: b"action_b" }` — open, all neurons except N have voted Yes.

**Attack:**
1. Governance's heartbeat triggers `process_proposals` → proposal A is adopted → `start_proposal_execution(A)` → `perform_execute_generic_nervous_system_function` → `env.call_canister(X, "execute", b"action_a")` → governance suspends.
2. Canister X's `execute` method receives `b"action_a"`. Before replying, it issues an inter-canister call to governance: `manage_neuron { subaccount: N.id, command: RegisterVote { proposal: B, vote: Yes } }`.
3. Governance processes the `manage_neuron` message: `register_vote` casts N's vote on B → `process_proposal(B)` → B reaches quorum → `start_proposal_execution(B)` → `perform_execute_generic_nervous_system_function` → `env.call_canister(X, "execute", b"action_b")` → governance suspends again.
4. Canister X's `execute` method receives `b"action_b"` and replies immediately.
5. Governance resumes proposal B's execution → `set_proposal_execution_status(B, Ok)` → B marked `Executed`.
6. Governance's `manage_neuron` reply returns to canister X.
7. Canister X replies to governance's original call for proposal A.
8. Governance resumes proposal A's execution → `set_proposal_execution_status(A, Ok)` → A marked `Executed`.

**Result:** Canister X received `b"action_b"` before `b"action_a"` completed — the intended execution order is violated, and canister X had full control over the interleaving.

### Citations

**File:** rs/sns/governance/src/canister_control.rs (L287-293)
```rust
    let result = env
        .call_canister(
            valid_function.target_canister_id,
            &valid_function.target_method,
            call.payload,
        )
        .await;
```

**File:** rs/sns/governance/src/governance.rs (L1960-2003)
```rust
        // If the status is open
        if proposal_data.status() != ProposalDecisionStatus::Open
            || !proposal_data.can_make_decision(now_seconds)
        {
            return;
        }

        // This marks the proposal_data as no longer open.
        proposal_data.decided_timestamp_seconds = now_seconds;
        if !proposal_data.is_accepted() {
            return;
        }

        // Return the rejection fee to the proposal's proposer
        if let Some(nid) = &proposal_data.proposer
            && let Some(neuron) = self.proto.neurons.get_mut(&nid.to_string())
            && neuron.neuron_fees_e8s >= proposal_data.reject_cost_e8s
        {
            neuron.neuron_fees_e8s -= proposal_data.reject_cost_e8s;
        }

        // A yes decision has been made, execute the proposal!
        // Safely unwrap action.
        let action = proposal_data
            .proposal
            .as_ref()
            .and_then(|p| p.action.clone());
        let action = match action {
            Some(action) => action,

            // This should not be possible, because proposal validation should
            // have been performed when the proposal was first made.
            None => {
                self.set_proposal_execution_status(
                    proposal_id,
                    Err(GovernanceError::new_with_message(
                        ErrorType::InvalidProposal,
                        "Proposal has no action.",
                    )),
                );
                return;
            }
        };
        self.start_proposal_execution(proposal_id, action);
```

**File:** rs/sns/governance/src/governance.rs (L2118-2134)
```rust
    fn start_proposal_execution(&mut self, proposal_id: u64, action: Action) {
        // `perform_action` is an async method of &mut self.
        //
        // Starting it and letting it run in the background requires knowing that
        // the `self` reference will last until the future has completed.
        //
        // The compiler cannot know that, but this is actually true:
        //
        // - in unit tests, all futures are immediately ready, because no real async
        //   call is made. In this case, the transmutation to a static ref is abusive,
        //   but it's still ok since the future will immediately resolve.
        //
        // - in prod, "self" is a reference to the GOVERNANCE static variable, which is
        //   initialized only once (in canister_init or canister_post_upgrade)
        let governance: &'static mut Governance = unsafe { std::mem::transmute(self) };
        spawn_in_canister_env(governance.perform_action(proposal_id, action));
    }
```

**File:** rs/sns/governance/src/governance.rs (L2531-2556)
```rust
    async fn perform_execute_generic_nervous_system_function(
        &self,
        call: ExecuteGenericNervousSystemFunction,
    ) -> Result<(), GovernanceError> {
        match self
            .proto
            .id_to_nervous_system_functions
            .get(&call.function_id)
        {
            None => Err(GovernanceError::new_with_message(
                ErrorType::NotFound,
                format!(
                    "There is no generic NervousSystemFunction with id: {}",
                    call.function_id
                ),
            )),
            Some(function) => {
                perform_execute_generic_nervous_system_function_call(
                    &*self.env,
                    function.clone(),
                    call,
                )
                .await
            }
        }
    }
```

**File:** rs/sns/governance/src/governance.rs (L3931-3946)
```rust
        Governance::cast_vote_and_cascade_follow(
            proposal_id,
            neuron_id,
            vote,
            function_id,
            &self.function_followee_index,
            &self.topic_follower_index,
            &self.proto.neurons,
            now_seconds,
            &mut proposal.ballots,
            proposal_topic.unwrap_or_default(),
        );

        self.process_proposal(proposal_id.id);

        Ok(())
```

**File:** rs/sns/governance/canister/canister.rs (L397-408)
```rust
#[update]
async fn manage_neuron(request: ManageNeuron) -> ManageNeuronResponse {
    log!(INFO, "manage_neuron");
    let governance = governance_mut();
    let result = measure_span_async(
        governance.profiling_information,
        "manage_neuron",
        governance.manage_neuron(&sns_gov_pb::ManageNeuron::from(request), &caller()),
    )
    .await;
    ManageNeuronResponse::from(result)
}
```
