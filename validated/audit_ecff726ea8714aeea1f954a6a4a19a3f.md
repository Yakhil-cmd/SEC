I have all the information needed. Let me analyze the key facts from the codebase:

**Fact 1**: `blob_store` has zero `inspect_message` exports — confirmed by grep returning no matches across all blob_store source files.

**Fact 2**: The IC runtime's `execute_inspect_message` explicitly passes all messages through when no export exists: [1](#0-0) 

**Fact 3**: Ingress induction cost is charged to the canister at induction time (before execution), not after: [2](#0-1) 

**Fact 4**: The cost formula is `1,200,000 + 2,000 × message_bytes` cycles per message: [3](#0-2) 

**Fact 5**: The `NotAuthorized` check in `insert()` happens at execution time — *after* the induction cost is already burned: [4](#0-3) 

**Fact 6**: The ingress selector's cumulative cycle check only prevents over-spending *within a single block*; it resets per block: [5](#0-4) 

---

### Title
Missing `inspect_message` Hook Enables Cycle-Drain via Unauthorized Large-Payload `insert` Calls — (`rs/cross-chain/blob_store/src/main.rs`)

### Summary

The `blob_store` canister exposes an `insert` update method restricted to controllers, but exports no `canister_inspect_message` hook. The IC protocol charges ingress induction cost to the canister at induction time, before execution. An unprivileged attacker can send repeated large-payload `insert` calls; each is inducted (burning cycles proportional to payload size), then rejected at execution with `NotAuthorized`. The canister's cycle balance is drained without any authorized action occurring.

### Finding Description

`rs/cross-chain/blob_store/src/main.rs` registers `insert` as an `#[ic_cdk::update]` with no `inspect_message` companion: [6](#0-5) 

When the IC runtime evaluates whether to accept an ingress message and the canister does not export `canister_inspect_message`, the filter returns `Ok(())` unconditionally: [1](#0-0) 

The message is then inducted and the canister is charged:

```
cost = 1_200_000 + 2_000 × raw_ingress_bytes
``` [7](#0-6) 

This charge is applied to the canister's balance immediately at induction: [8](#0-7) 

Only after the charge is burned does execution begin, where `is_controller` is checked and `NotAuthorized` is returned: [4](#0-3) 

The IC's maximum ingress message size is 2 MB. A 2 MB payload costs approximately:

```
1_200_000 + 2_000 × 2_000_000 = ~4,001,200,000 cycles per message
```

At ~1 block/second, a sustained attacker sending one max-size message per block drains ~4 billion cycles/second. A canister holding 1 trillion cycles would be frozen in ~250 seconds.

### Impact Explanation

If the `blob_store` canister is frozen (cycle balance falls below freeze threshold), all `get()` queries become unavailable. Cross-chain upgrade orchestrators that depend on `get()` to retrieve upgrade WASMs cannot proceed, potentially locking cross-chain assets in an unupgradeable state. [9](#0-8) 

### Likelihood Explanation

The attack requires no privilege — any principal, including anonymous, can submit ingress messages to any canister. The only practical throttle is boundary-node rate limiting, which is a best-effort mechanism and not a security guarantee. The attack is fully local-testable with a state-machine test.

### Recommendation

Add an `inspect_message` hook that rejects non-controller callers before induction cost is charged:

```rust
#[ic_cdk::inspect_message]
fn inspect_message() {
    if ic_cdk::api::is_controller(&ic_cdk::api::msg_caller()) {
        ic_cdk::api::call::accept_message();
    }
    // implicit trap/reject for non-controllers
}
```

This mirrors the pattern used by other security-sensitive canisters in the same repo: [10](#0-9) 

### Proof of Concept

State-machine test outline:
1. Deploy `blob_store` canister with a known cycle balance B.
2. Send N `insert` update calls from `Principal::anonymous()` with 2 MB payloads each.
3. After execution, read the canister's cycle balance B'.
4. Assert `B - B' ≈ N × 4_001_200_000` cycles.
5. Show that for sufficiently large N, `B' < freeze_threshold`, causing the canister to be frozen and `get()` queries to fail.

### Citations

**File:** rs/execution_environment/src/execution/inspect_message.rs (L56-60)
```rust
    // If the Wasm module does not export the method, then this execution
    // succeeds as a no-op.
    if !execution_state.exports_method(&method) {
        return (message_instruction_limit, Ok(()));
    }
```

**File:** rs/messaging/src/scheduling/valid_set_rule.rs (L293-315)
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
```

**File:** rs/config/src/subnet_config.rs (L505-515)
```rust
            update_message_execution_fee: Cycles::new(5_000_000),
            ten_update_instructions_execution_fee: Cycles::new(
                ten_update_instructions_execution_fee_in_cycles,
            ),
            ten_update_instructions_execution_fee_wasm64: Cycles::new(
                WASM64_INSTRUCTION_COST_OVERHEAD * ten_update_instructions_execution_fee_in_cycles,
            ),
            xnet_call_fee: Cycles::new(260_000),
            xnet_byte_transmission_fee: Cycles::new(1_000),
            ingress_message_reception_fee: Cycles::new(1_200_000),
            ingress_byte_reception_fee: Cycles::new(2_000),
```

**File:** rs/cross-chain/blob_store/src/update.rs (L11-13)
```rust
    if !ic_cdk::api::is_controller(&caller) {
        return Err(InsertError::NotAuthorized);
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

**File:** rs/cross-chain/blob_store/src/main.rs (L20-29)
```rust
#[ic_cdk::update]
fn insert(request: InsertRequest) -> Result<String, InsertError> {
    blob_store_lib::update::insert(
        ic_cdk::api::msg_caller(),
        &request.hash,
        request.data,
        request.tags.unwrap_or_default(),
    )
    .map(|hash| hash.to_string())
}
```

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L599-607)
```rust
    /// Returns the cost of an ingress message based on the message size.
    pub fn ingress_induction_cost_from_bytes(
        &self,
        bytes: NumBytes,
        subnet_cycles_config: CyclesAccountManagerSubnetConfig,
    ) -> CompoundCycles<IngressInduction> {
        self.ingress_message_received_fee(subnet_cycles_config)
            + self.ingress_byte_received_fee(subnet_cycles_config) * bytes.get()
    }
```

**File:** rs/cross-chain/blob_store/blob_store.did (L46-56)
```text
service : () -> {
    // Retrieves a blob by its hex-encoded SHA-256 hash. Can be called by anyone.
    get : (text) -> (variant { Ok : blob; Err : GetError }) query;

    // Retrieves blob metadata by its hex-encoded SHA-256 hash. Can be called by anyone.
    get_metadata : (text) -> (variant { Ok : BlobMetadata; Err : GetError }) query;

    // Stores a blob. Only canister controllers are authorized to call this method.
    // The caller must provide the expected SHA-256 hash of the data.
    insert : (InsertRequest) -> (variant { Ok : HexHash; Err : InsertError });
}
```

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L34-67)
```rust
#[inspect_message]
fn inspect_message() {
    // In order for this hook to succeed, accept_message() must be invoked.
    let caller_id: Principal = ic_cdk::api::caller();
    let called_method = ic_cdk::api::call::method_name();

    let (has_full_access, has_full_read_access) = with_canister_state(|state| {
        let authorized_principal = state.get_authorized_principal();
        (
            Some(caller_id) == authorized_principal,
            state.is_api_boundary_node_principal(&caller_id),
        )
    });

    if called_method == REPLICATED_QUERY_METHOD {
        if has_full_access || has_full_read_access {
            ic_cdk::api::call::accept_message();
        } else {
            ic_cdk::api::trap(
                "message_inspection_failed: method call is prohibited in the current context",
            );
        }
    } else if UPDATE_METHODS.contains(&called_method.as_str()) {
        if has_full_access {
            ic_cdk::api::call::accept_message();
        } else {
            ic_cdk::api::trap("message_inspection_failed: unauthorized caller");
        }
    } else {
        // All others calls are rejected
        ic_cdk::api::trap(
            "message_inspection_failed: method call is prohibited in the current context",
        );
    }
```
