The vulnerability is real and concrete. Let me confirm the exact line and the IC's cycle accounting behavior before writing the final answer.

The key facts established:

1. **`rs/sns/swap/canister/canister.rs:133`**: `PrincipalId::from_str(&arg.buyer).unwrap()` — bare `unwrap()` on attacker-controlled string, no access control on the `#[update]` endpoint.

2. **IC cycle accounting** (`rs/messaging/src/scheduling/valid_set_rule.rs:305-314`, `rs/cycles_account_manager/src/cycles_account_manager.rs:583-584`): ingress induction cost is charged to the **receiving canister** at induction time, before execution. Execution cycles are also charged even on trap.

3. **Ingress selector** (`rs/ingress_manager/src/ingress_selector.rs:566-583`): only rejects new messages if the canister already lacks cycles — it does not reject messages with invalid payloads.

---

### Title
Unprivileged Caller Can Trigger Canister Trap via Invalid `buyer` Field in `refresh_buyer_tokens`, Draining Swap Canister Cycles — (`rs/sns/swap/canister/canister.rs`)

### Summary
The `refresh_buyer_tokens` update endpoint unconditionally calls `.unwrap()` on an attacker-supplied string, causing a Wasm trap on any non-empty, non-valid `PrincipalId` string. Because the IC charges ingress induction cost and execution cost to the receiving canister even when it traps, any unprivileged user can repeatedly trigger this to drain the swap canister's cycle balance.

### Finding Description
In `rs/sns/swap/canister/canister.rs`, the public `#[update]` handler `refresh_buyer_tokens` contains:

```rust
// rs/sns/swap/canister/canister.rs:128-134
#[update]
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    log!(INFO, "refresh_buyer_tokens");
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()   // ← panics on invalid input
    };
``` [1](#0-0) 

`RefreshBuyerTokensRequest.buyer` is a plain protobuf `string` field. Any caller can set it to an arbitrary byte sequence (e.g., `"not-a-principal"` or a 1000-byte string). When `arg.buyer` is non-empty and not a valid textual `PrincipalId`, `PrincipalId::from_str` returns `Err`, and `.unwrap()` panics, trapping the message.

There is no access control on this endpoint — it is callable by any anonymous or authenticated principal.

### Impact Explanation
On the Internet Computer, ingress induction cost is charged to the **receiving canister** at induction time, before execution begins: [2](#0-1) [3](#0-2) 

Execution cycles are also charged for instructions executed up to the trap point. Both costs are non-refundable on trap. An attacker sending a stream of `RefreshBuyerTokensRequest{buyer: "x"}` messages causes the swap canister to pay induction + execution cost for each, with zero benefit. Sustained at the subnet's ingress rate limit, this drains the canister's cycle balance. Once the balance falls below the freeze threshold, the ingress selector begins rejecting new messages (`InsufficientCycles`), effectively freezing the swap canister and preventing legitimate participation. [4](#0-3) 

### Likelihood Explanation
- No privilege required; the endpoint is fully public.
- The attack payload is trivial: a single-byte non-principal string in the `buyer` field.
- The trap occurs before any async ledger call, so each malicious message is cheap to send and cheap to execute (fast trap), maximizing the attacker's cycle-drain rate relative to their own cost (which is zero — ingress on IC has no sender-side fee).
- The swap canister is a time-limited, high-value target; draining it during an active swap window has direct financial impact on the SNS launch.

### Recommendation
Replace the bare `.unwrap()` with proper error handling that returns a user-visible error instead of panicking:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    match PrincipalId::from_str(&arg.buyer) {
        Ok(p) => p,
        Err(e) => ic_cdk::trap(&format!("Invalid buyer principal: {}", e)),
        // or return an Err variant if the return type is changed to Result
    }
};
```

`ic_cdk::trap` still traps (and still costs cycles), so the better fix is to return a structured error response without trapping at all, which avoids the execution cost on bad input. Ideally, validate and reject at the Candid/protobuf decode boundary or return `Err` from the handler.

### Proof of Concept
1. Deploy the swap canister in `Open` lifecycle state.
2. Send repeated ingress calls to `refresh_buyer_tokens` with `RefreshBuyerTokensRequest { buyer: "not-a-principal", confirmation_text: None }`.
3. Each call traps with `CanisterCalledTrap`; observe the canister's cycle balance decreasing with each call.
4. Confirm no state mutation occurs (rollback), but cycles are consumed.

Unit-level proof: call the canister-level handler directly in a state-machine test with `buyer = "x"` and assert the result is `WasmResult::Reject` with `CanisterCalledTrap`, while the canister balance decreases. [5](#0-4)

### Citations

**File:** rs/sns/swap/canister/canister.rs (L128-134)
```rust
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    log!(INFO, "refresh_buyer_tokens");
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()
    };
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

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L583-584)
```rust
            // A message to a canister is always paid for by the receiving canister.
            false => Some(ingress.canister_id()),
```

**File:** rs/ingress_manager/src/ingress_selector.rs (L561-583)
```rust
            IngressInductionCost::Fee {
                payer,
                cost: ingress_cost,
            } => match state.canister_state(&payer) {
                Some(canister) => {
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
```
