### Title
Authorization Bypass via Inter-Canister Calls in `inspect_message`-Only Gated Canisters — (`rs/boundary_node/rate_limits/canister/canister.rs`, `rs/boundary_node/salt_sharing/canister/canister.rs`)

---

### Summary

The rate-limit canister and salt-sharing canister implement all caller authorization exclusively inside `canister_inspect_message`. The IC protocol explicitly does **not** invoke `canister_inspect_message` for inter-canister calls. Any deployed canister can therefore call the restricted methods (`add_config`, `disclose_rules`, `get_config`, `get_salt`) directly via inter-canister update calls, bypassing all authorization checks.

This is structurally analogous to the Sui `id_leak_verifier` bypass: in Sui, the verifier only checks types that carry the `key` capability, so old-version objects without `key` bypass the check. In the IC, `inspect_message` only checks ingress messages, so inter-canister callers bypass the check entirely.

---

### Finding Description

**Rate-limit canister** — the entire authorization surface is:

```rust
// rs/boundary_node/rate_limits/canister/canister.rs:30-68
#[inspect_message]
fn inspect_message() {
    let caller_id: Principal = ic_cdk::api::caller();
    let called_method = ic_cdk::api::call::method_name();
    let (has_full_access, has_full_read_access) = with_canister_state(|state| { ... });

    if called_method == REPLICATED_QUERY_METHOD {          // "get_config"
        if has_full_access || has_full_read_access { accept_message(); }
        else { ic_cdk::api::trap("..."); }
    } else if UPDATE_METHODS.contains(&called_method.as_str()) { // "add_config", "disclose_rules"
        if has_full_access { accept_message(); }
        else { ic_cdk::api::trap("..."); }
    } else {
        ic_cdk::api::trap("...");
    }
}
``` [1](#0-0) 

**Salt-sharing canister** — same pattern:

```rust
// rs/boundary_node/salt_sharing/canister/canister.rs:20-29
#[inspect_message]
fn inspect_message() {
    if called_method == REPLICATED_QUERY_METHOD && is_api_boundary_node_principal(&caller_id) {
        accept_message();
    } else {
        trap("message_inspection_failed: ...");
    }
}
``` [2](#0-1) 

The IC execution environment explicitly documents and implements this gap — `execute_inspect_message` is only invoked from `should_accept_ingress_message`, which is only reached for ingress (user-originated) messages:

```rust
// rs/execution_environment/src/execution_environment.rs:3376-3383
if ingress.content().is_addressed_to_subnet() {
    return self.canister_manager.should_accept_ingress_message(...);
}
// ... then calls execute_inspect_message
``` [3](#0-2) 

The test suite confirms this is by design — inter-canister calls bypass `inspect_message` even when it rejects all ingress:

```rust
// rs/execution_environment/src/execution_environment/tests.rs:2754-2773
// Inter-canister calls still work.
let caller = test.universal_canister().unwrap();
let res = test.ingress(caller, "update",
    wasm().inter_update(canister, CallArgs::default()).build()).unwrap();
``` [4](#0-3) 

---

### Impact Explanation

**Rate-limit canister:**
- A malicious canister can call `add_config` via inter-canister update, injecting arbitrary rate-limit rules without being the authorized principal. This can suppress or manipulate boundary-node rate limiting for targeted principals.
- A malicious canister can call `get_config` via inter-canister update, reading the full undisclosed rate-limit configuration (including rules for unrevealed security incidents).
- A malicious canister can call `disclose_rules` to prematurely expose incident-linked rules.

**Salt-sharing canister:**
- A malicious canister can call `get_salt` via inter-canister update, obtaining the privacy salt used for user pseudonymization at boundary nodes. Possession of this salt enables deanonymization of users across sessions.

Both impacts are canister isolation breaks with direct protocol-level consequences.

---

### Likelihood Explanation

Deploying a canister on the IC is permissionless. Any principal can deploy a canister on any application subnet and issue inter-canister calls to canisters on other subnets (including the II subnet where the rate-limit canister resides). No privileged access, governance majority, or threshold corruption is required. The attack requires only knowledge of the target canister ID and method names, both of which are public.

---

### Recommendation

Authorization must be enforced inside the actual method handlers, not only in `canister_inspect_message`. The `inspect_message` hook is explicitly a pre-consensus ingress filter and provides no protection against inter-canister calls. Each restricted method (`add_config`, `disclose_rules`, `get_config`, `get_salt`) must independently verify `ic_cdk::api::caller()` against the authorized principal set before executing any logic.

---

### Proof of Concept

```rust
// Attacker canister deployed on any subnet
#[update]
async fn exploit_rate_limit() {
    // Bypasses inspect_message entirely — no authorization check fires
    let _: () = ic_cdk::call(
        rate_limit_canister_id,
        "add_config",
        (malicious_config,),
    ).await.unwrap();
}

#[update]
async fn steal_salt() {
    // Bypasses inspect_message entirely
    let (salt,): (SaltResponse,) = ic_cdk::call(
        salt_sharing_canister_id,
        "get_salt",
        (),
    ).await.unwrap();
    // salt is now in attacker's hands
}
```

The `inspect_message` hook at [5](#0-4)  and [2](#0-1)  is never invoked for these calls. The execution path through `execute_inspect_message` at [6](#0-5)  is only reachable from the ingress filter path, not from inter-canister message routing.

### Citations

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L30-68)
```rust
const UPDATE_METHODS: [&str; 2] = ["add_config", "disclose_rules"];
const REPLICATED_QUERY_METHOD: &str = "get_config";

// Inspect the ingress messages in the pre-consensus phase and reject early, if the conditions are not met
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
}
```

**File:** rs/boundary_node/salt_sharing/canister/canister.rs (L20-29)
```rust
#[inspect_message]
fn inspect_message() {
    let caller_id = caller();
    let called_method = method_name();

    if called_method == REPLICATED_QUERY_METHOD && is_api_boundary_node_principal(&caller_id) {
        accept_message();
    } else {
        trap("message_inspection_failed: method call is prohibited in the current context");
    }
```

**File:** rs/execution_environment/src/execution_environment.rs (L3376-3383)
```rust
        if ingress.content().is_addressed_to_subnet() {
            return self.canister_manager.should_accept_ingress_message(
                state,
                provisional_whitelist,
                ingress.content(),
                effective_canister_id,
            );
        }
```

**File:** rs/execution_environment/src/execution_environment/tests.rs (L2754-2773)
```rust
    // Inter-canister calls still work.
    let caller = test.universal_canister().unwrap();
    let res = test
        .ingress(
            caller,
            "update",
            wasm().inter_update(canister, CallArgs::default()).build(),
        )
        .unwrap();
    let expected_reply = [
        b"Hello ",
        caller.get().as_slice(),
        b" this is ",
        canister.get().as_slice(),
    ]
    .concat();
    match res {
        WasmResult::Reply(data) => assert_eq!(data, expected_reply),
        WasmResult::Reject(msg) => panic!("Unexpected reject: {msg}"),
    };
```

**File:** rs/execution_environment/src/execution/inspect_message.rs (L22-60)
```rust
pub fn execute_inspect_message(
    time: Time,
    canister: CanisterState,
    ingress: &SignedIngressContent,
    execution_parameters: ExecutionParameters,
    subnet_available_memory: SubnetAvailableMemory,
    hypervisor: &Hypervisor,
    network_topology: &NetworkTopology,
    logger: &ReplicaLogger,
    state_changes_error: &IntCounter,
    ingress_filter_metrics: &IngressFilterMetrics,
    subnet_cycles_config: CyclesAccountManagerSubnetConfig,
) -> (NumInstructions, Result<(), UserError>) {
    let canister_id = canister.canister_id();
    let memory_usage = canister.memory_usage();
    let message_memory_usage = canister.message_memory_usage();
    let method = WasmMethod::System(SystemMethod::CanisterInspectMessage);
    let (execution_state, system_state, _) = canister.into_parts();
    let message_instruction_limit = execution_parameters.instruction_limits.message();

    // Validate that the Wasm module is present.
    let execution_state = match execution_state {
        None => {
            return (
                message_instruction_limit,
                Err(UserError::new(
                    ErrorCode::CanisterWasmModuleNotFound,
                    "Requested canister has no wasm module",
                )),
            );
        }
        Some(execution_state) => execution_state,
    };

    // If the Wasm module does not export the method, then this execution
    // succeeds as a no-op.
    if !execution_state.exports_method(&method) {
        return (message_instruction_limit, Ok(()));
    }
```
