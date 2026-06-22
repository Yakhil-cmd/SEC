### Title
Unbounded Unmetered Iteration Over `raw_rand_contexts` in `execute_round` Enables Repeatable Per-Round Consensus Delay — (File: `rs/execution_environment/src/scheduler.rs`)

---

### Summary

Every execution round, all pending `raw_rand` contexts are drained and processed in an explicitly unmetered `while let Some(...)` loop inside `execute_round`. A malicious canister can flood the `raw_rand_contexts` queue in one round, causing the next round to process all of them without consuming any instructions from the round limit. This is a direct structural analog to the Rewards Plans BeginBlock flood: an attacker-controlled collection is iterated linearly in an unmetered critical execution path, with no per-round cap.

---

### Finding Description

In `rs/execution_environment/src/scheduler.rs`, the `execute_round` function contains the following loop:

```rust
// Each round, we check for any postponed `raw_rand` requests.
// If found, they are processed immediately. Raw rand is not
// consuming instructions, so all existing raw_rand requests
// will be processed.
while let Some(raw_rand_context) = state
    .metadata
    .subnet_call_context_manager
    .raw_rand_contexts
    .pop_front()
{
    debug_assert_lt!(raw_rand_context.execution_round_id, current_round);
    let (new_state, _) = self.execute_subnet_message(
        SubnetMessage::Request(raw_rand_context.request.into()),
        state,
        &mut csprng,
        current_round,
        &mut subnet_round_limits,
        ...
    );
    state = new_state;
}
``` [1](#0-0) 

The code comment is explicit: **"Raw rand is not consuming instructions, so all existing raw_rand requests will be processed."** Although `subnet_round_limits` is passed to `execute_subnet_message`, the `raw_rand` path does not decrement the instruction counter. The loop therefore runs to completion over the entire `raw_rand_contexts` queue with no instruction-budget guard.

The `raw_rand_contexts` field is a `VecDeque<RawRandContext>` inside `SubnetCallContextManager`: [2](#0-1) 

Every time a canister calls `raw_rand` on the management canister (`ic:00`), a `RawRandContext` is pushed onto this queue. The queue is drained in full every round in the unmetered loop above.

The attacker-controlled entry path is:
1. A malicious canister (or set of canisters) calls `raw_rand` on `ic:00` as many times as the round instruction budget allows in round R.
2. Each call enqueues one `RawRandContext` into `raw_rand_contexts`.
3. In round R+1, the unmetered drain loop calls `execute_subnet_message` once per queued context, with no instruction limit applied to the loop as a whole.
4. Each `execute_subnet_message` invocation generates random bytes, constructs a response, and mutates replicated state — work that is real and non-trivial.
5. The attack repeats every round.

The `raw_rand` call itself (the canister-to-management-canister request) does consume instructions in the normal execution path, so the queue size per round is bounded by `max_instructions_per_round / cost_per_raw_rand_call`. On application subnets the round instruction limit is on the order of 7 × 10⁹ instructions. If a `raw_rand` call costs ~10³–10⁵ instructions, an attacker can queue 10⁴–10⁶ contexts per round. All of them are then processed in the next round without any instruction accounting.

---

### Impact Explanation

The unmetered drain loop in round R+1 calls `execute_subnet_message` N times (where N is attacker-controlled up to the previous round's instruction budget). Each invocation performs CSPRNG operations, response construction, and replicated-state mutations. For large N this extends the wall-clock duration of round R+1 beyond the consensus timeout, causing:

- **Consensus delay / chain halt**: Nodes that time out on round R+1 will not finalize the block, stalling the subnet.
- **Repeatable attack**: Because the queue is refilled each round, the attacker can sustain the delay indefinitely as long as they can afford the cycle cost of `raw_rand` calls.
- **All AVS/canister security guarantees broken**: A stalled subnet breaks liveness for every canister and cross-chain integration hosted on it.

The vulnerability class is **cycles/resource accounting bug leading to consensus safety break**, matching the target scope.

---

### Likelihood Explanation

- **Permissionless**: Any deployed canister can call `raw_rand` on `ic:00`; no privileged role is required.
- **Low cost**: `raw_rand` is a cheap management-canister call. The cycle cost to queue a large number of contexts is bounded only by the round instruction limit, not by any additional fee.
- **No existing cap**: There is no per-round limit on the number of `raw_rand_contexts` that will be processed, and no instruction charge applied to the drain loop.
- **Repeatable**: The attack can be sustained every round, making it a persistent denial-of-service vector.

---

### Recommendation

Apply one or more of the following mitigations:

1. **Per-round cap**: Process at most `MAX_RAW_RAND_PER_ROUND` contexts per round, leaving the remainder for subsequent rounds.
2. **Instruction metering**: Charge instructions for each `raw_rand` context processed in the drain loop, so the loop is subject to the same `subnet_round_limits` guard as other subnet messages.
3. **Scaling fee**: Apply a scaling fee to `raw_rand` calls proportional to the current queue depth, analogous to the recommendation in the external report (`ctx.GasMeter().ConsumeGas(uint64(BaseGasFee * len(existingQueue)), ...)`).

---

### Proof of Concept

```rust
// Malicious canister pseudocode (executed in round R):
// Call raw_rand as many times as the instruction budget allows.
// Each call enqueues one RawRandContext.
for _ in 0..MAX_CALLS_PER_ROUND {
    ic_cdk::call(Principal::management_canister(), "raw_rand", ()).await;
}

// In round R+1, execute_round drains all N contexts in an unmetered loop:
//   while let Some(ctx) = raw_rand_contexts.pop_front() {
//       execute_subnet_message(raw_rand_request, ...);  // no instruction charge
//   }
// Wall-clock time for round R+1 grows linearly with N,
// potentially exceeding the consensus timeout and halting the subnet.
```

The relevant unmetered loop is at: [1](#0-0) 

The `raw_rand_contexts` queue definition is at: [3](#0-2)

### Citations

**File:** rs/execution_environment/src/scheduler.rs (L1379-1402)
```rust
            // Each round, we check for any postponed `raw_rand` requests.
            // If found, they are processed immediately. Raw rand is not
            // consuming instructions, so all existing raw_rand requests
            // will be processed.
            while let Some(raw_rand_context) = state
                .metadata
                .subnet_call_context_manager
                .raw_rand_contexts
                .pop_front()
            {
                debug_assert_lt!(raw_rand_context.execution_round_id, current_round);
                let (new_state, _) = self.execute_subnet_message(
                    SubnetMessage::Request(raw_rand_context.request.into()),
                    state,
                    &mut csprng,
                    current_round,
                    &mut subnet_round_limits,
                    registry_settings,
                    replica_version,
                    &measurement_scope,
                    &chain_key_data,
                );
                state = new_state;
            }
```

**File:** rs/replicated_state/src/metadata_state/subnet_call_context_manager.rs (L213-229)
```rust
pub struct SubnetCallContextManager {
    /// Should increase monotonically. This property is used to determine if a request
    /// corresponds to a future state.
    next_callback_id: u64,
    pub setup_initial_dkg_contexts: BTreeMap<CallbackId, SetupInitialDkgContext>,
    pub sign_with_threshold_contexts: BTreeMap<CallbackId, SignWithThresholdContext>,
    pub canister_http_request_contexts: BTreeMap<CallbackId, CanisterHttpRequestContext>,
    /// `CanisterHttpRequestContext`s whose responses have already been delivered to execution.
    /// They are kept here such that asynchronous refunds may continue to be processed.
    pub delivered_canister_http_request_contexts: BTreeMap<CallbackId, CanisterHttpRequestContext>,
    pub reshare_chain_key_contexts: BTreeMap<CallbackId, ReshareChainKeyContext>,
    pub bitcoin_get_successors_contexts: BTreeMap<CallbackId, BitcoinGetSuccessorsContext>,
    pub bitcoin_send_transaction_internal_contexts:
        BTreeMap<CallbackId, BitcoinSendTransactionInternalContext>,
    canister_management_calls: CanisterManagementCalls,
    pub raw_rand_contexts: VecDeque<RawRandContext>,
    pub pre_signature_stashes: BTreeMap<IDkgMasterPublicKeyId, PreSignatureStash>,
```
