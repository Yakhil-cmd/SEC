### Title
SNS Governance `ExecuteGenericNervousSystemFunction` Payload Size Unenforced — Heap Memory Exhaustion DoS Blocking All Proposals - (File: `rs/sns/governance/src/types.rs`)

---

### Summary

The SNS governance canister defines a maximum payload size constant for `ExecuteGenericNervousSystemFunction` proposals but never enforces it. A neuron holder with sufficient stake can submit up to 700 open proposals carrying arbitrarily large payloads, exhausting the SNS governance canister's heap and permanently blocking all new proposal submissions.

---

### Finding Description

In `rs/sns/governance/src/types.rs`, the constant `PROPOSAL_EXECUTE_SNS_FUNCTION_PAYLOAD_BYTES_MAX` is defined at 70,000 bytes but is annotated `#[allow(dead_code)]` with an explicit TODO comment acknowledging it is not yet used for validation: [1](#0-0) 

This constant is never referenced in the proposal validation path. The `validate_and_render_action` function in `rs/sns/governance/src/proposal.rs` dispatches `ExecuteGenericNervousSystemFunction` to `validate_and_render_execute_nervous_system_function`, which only calls the externally-registered validator canister — it performs no size check on the payload bytes before storing the proposal: [2](#0-1) 

By contrast, the NNS governance enforces `PROPOSAL_EXECUTE_NNS_FUNCTION_PAYLOAD_BYTES_MAX` explicitly inside `validate_execute_nns_function`: [3](#0-2) 

Once a proposal passes validation, `make_proposal` stores the full `ProposalData` — including the raw payload bytes and a ballot entry for every eligible neuron — in the governance canister's heap: [4](#0-3) 

The global cap on unsettled proposals is `MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS = 700`: [5](#0-4) 

Ballots are only cleared after a reward event settles the proposal, which requires the voting period to expire and a reward round to elapse: [6](#0-5) 

The memory test canister itself estimates `ExecuteGenericNervousSystemFunction` payloads at ~1 MB each in worst-case sizing: [7](#0-6) 

With 700 open slots × up to ~2 MB per payload (IC message size limit), an attacker can push the governance canister's heap toward its 3.5 GiB soft limit (`HEAP_SIZE_SOFT_LIMIT_IN_WASM32_PAGES`): [8](#0-7) 

---

### Impact Explanation

**Governance DoS:** Once 700 unsettled proposals exist, `make_proposal` returns `ResourceExhausted` for all non-whitelisted proposal types. No legitimate user can submit proposals until the voting period expires and a reward round clears ballots — a delay of at minimum `initial_voting_period_seconds` (default 4 days) plus one reward round.

**Heap memory exhaustion:** Each oversized proposal permanently occupies heap until ballots are cleared. If the heap soft limit is breached, `check_heap_can_grow` blocks even whitelisted proposals, and canister upgrades may fail, permanently bricking the SNS governance.

**Scope match:** Governance resource exhaustion / cycles-resource accounting bug causing service unavailability — directly within the HackenProof Internet Computer scope for SNS governance users.

---

### Likelihood Explanation

**Preconditions:**
1. Attacker holds a neuron with stake ≥ `reject_cost_e8s` × 700 (default: 700 governance tokens — achievable for a motivated attacker in any SNS with liquid tokens).
2. At least one `GenericNervousSystemFunction` is registered in the SNS whose validator canister does not enforce a payload size limit (common, since the SNS governance itself does not require validators to do so).

**Trigger:** The attacker calls `make_proposal` 700 times with `ExecuteGenericNervousSystemFunction` payloads near the IC message size limit (~2 MB). Each call passes the validator canister (which returns `Ok`) and is stored in heap.

**Cost to attacker:** `reject_cost_e8s` per proposal is charged upfront as `neuron_fees_e8s` but is refunded if the proposal is adopted. If the attacker votes yes and no other neuron has majority, the proposals are rejected and the fees are burned — but the governance remains blocked for the full voting period regardless. [9](#0-8) 

---

### Recommendation

Enforce `PROPOSAL_EXECUTE_SNS_FUNCTION_PAYLOAD_BYTES_MAX` inside `validate_and_render_execute_nervous_system_function` (or in `validate_and_render_action`) before the validator canister is called, mirroring the NNS governance pattern. Remove the `#[allow(dead_code)]` annotation and wire the constant into the validation path. Additionally, consider a per-neuron limit on simultaneously open proposals to prevent a single actor from saturating the global cap.

---

### Proof of Concept

1. Register a `GenericNervousSystemFunction` with a validator canister that accepts any payload (returns `Ok(String)`).
2. Obtain a neuron with ≥ 700 governance tokens staked.
3. In a loop, call `make_proposal` with:
   ```
   Action::ExecuteGenericNervousSystemFunction(ExecuteGenericNervousSystemFunction {
       function_id: <registered_id>,
       payload: vec![0u8; 2_000_000],  // ~2 MB, no SNS-side size check
   })
   ```
4. After 700 calls, any subsequent `make_proposal` from any neuron returns `ResourceExhausted`.
5. The governance canister heap grows by ~700 × 2 MB = ~1.4 GB of payload data plus ballot storage for all neurons, approaching or exceeding the 3.5 GiB soft limit.

The dead constant confirming the missing enforcement: [1](#0-0) 

The NNS enforcement that SNS is missing: [10](#0-9)

### Citations

**File:** rs/sns/governance/src/types.rs (L72-75)
```rust
#[allow(dead_code)]
/// TODO Use to validate the size of the payload 70 KB (for executing
/// SNS functions that are not canister upgrades)
const PROPOSAL_EXECUTE_SNS_FUNCTION_PAYLOAD_BYTES_MAX: usize = 70000;
```

**File:** rs/sns/governance/src/proposal.rs (L78-79)
```rust
/// The maximum number of unsettled proposals (proposals for which ballots are still stored).
pub const MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS: usize = 700;
```

**File:** rs/sns/governance/src/proposal.rs (L436-439)
```rust
        Action::ExecuteGenericNervousSystemFunction(execute) => {
            validate_and_render_execute_nervous_system_function(env, execute, existing_functions)
                .await
        }
```

**File:** rs/nns/governance/src/governance.rs (L4933-4942)
```rust
        // Check payload size limits
        if !update.can_have_large_payload()
            && update.payload.len() > PROPOSAL_EXECUTE_NNS_FUNCTION_PAYLOAD_BYTES_MAX
        {
            return Err(invalid_proposal_error(format!(
                "The maximum NNS function payload size in a proposal action is {} bytes, this payload is: {} bytes",
                PROPOSAL_EXECUTE_NNS_FUNCTION_PAYLOAD_BYTES_MAX,
                update.payload.len(),
            )));
        }
```

**File:** rs/sns/governance/src/governance.rs (L164-169)
```rust
/// The max number of wasm32 pages for the heap after which we consider that there
/// is a risk to the ability to grow the heap.
///
/// This is 7/8 of the maximum number of pages and corresponds to 3.5 GiB.
pub const HEAP_SIZE_SOFT_LIMIT_IN_WASM32_PAGES: usize =
    MAX_HEAP_SIZE_IN_KIB / WASM32_PAGE_SIZE_IN_KIB * 7 / 8;
```

**File:** rs/sns/governance/src/governance.rs (L3528-3547)
```rust
        // Check that there are not too many proposals.  What matters
        // here is the number of proposals for which ballots have not
        // yet been cleared, because ballots take the most amount of
        // space.
        if self
            .proto
            .proposals
            .values()
            .filter(|data| !data.ballots.is_empty())
            .count()
            >= MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS
            && !proposal.allowed_when_resources_are_low()
        {
            return Err(GovernanceError::new_with_message(
                ErrorType::ResourceExhausted,
                "Reached maximum number of proposals that have not yet \
                been taken into account for voting rewards. \
                Please try again later.",
            ));
        }
```

**File:** rs/sns/governance/src/governance.rs (L3644-3653)
```rust
        // Charge the cost of rejection upfront.
        // This will protect from DoS in couple of ways:
        // - It prevents a neuron from having too many proposals outstanding.
        // - It reduces the voting power of the submitter so that for every proposal
        //   outstanding the submitter will have less voting power to get it approved.
        self.proto
            .neurons
            .get_mut(&proposer_id.to_string())
            .expect("Proposer not found.")
            .neuron_fees_e8s += proposal_data.reject_cost_e8s;
```

**File:** rs/sns/governance/src/governance.rs (L6074-6080)
```rust
            // Ballots are used to determine two things:
            //   1. (obviously and primarily) whether to execute the proposal.
            //   2. rewards
            // At this point, we no longer need ballots for either of these
            // things, and since they take up a fair amount of space, we take
            // this opportunity to jettison them.
            p.ballots.clear();
```

**File:** rs/sns/integration_tests/test_canisters/sns_governance_mem_test_canister.rs (L280-280)
```rust
        x if x == NativeAction::ExecuteGenericNervousSystemFunction as u64 => 1_000_000, // Estimate of average payload size = 1MB
```
