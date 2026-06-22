### Title
SNS Governance `ExecuteGenericNervousSystemFunction` Cannot Attach Cycles to Inter-Canister Calls - (File: `rs/sns/governance/src/canister_control.rs`)

---

### Summary

The SNS governance canister's `ExecuteGenericNervousSystemFunction` proposal type executes inter-canister calls via `env.call_canister()`, which has no cycles parameter. Neither the `ExecuteGenericNervousSystemFunction` payload struct nor the `Environment::call_canister` trait method supports specifying cycles to attach. As a result, any SNS governance proposal that needs to call a canister method requiring cycle payment cannot be executed through this mechanism.

---

### Finding Description

When an `ExecuteGenericNervousSystemFunction` proposal is adopted, SNS governance calls `perform_execute_generic_nervous_system_function_call` in `rs/sns/governance/src/canister_control.rs`:

```rust
let result = env
    .call_canister(
        valid_function.target_canister_id,
        &valid_function.target_method,
        call.payload,
    )
    .await;
``` [1](#0-0) 

The `Environment` trait's `call_canister` signature (as seen in the SNS root environment definition) takes only `canister_id`, `method_name`, and `arg` — no cycles parameter:

```rust
async fn call_canister(
    &self,
    canister_id: CanisterId,
    method_name: &str,
    arg: Vec<u8>,
) -> Result<Vec<u8>, (i32, String)>;
``` [2](#0-1) 

The production canister implementation (e.g., in `rs/nns/integration_tests/test_canisters/unstoppable_sns_root_canister.rs`) confirms this uses `call_bytes_with_cleanup`, which attaches zero cycles: [3](#0-2) 

Furthermore, the `ExecuteGenericNervousSystemFunction` struct itself has no field for specifying cycles to attach:

```rust
pub struct ExecuteGenericNervousSystemFunction {
    pub function_id: u64,
    pub payload: Vec<u8>,
}
``` [4](#0-3) 

The `perform_action` dispatch in SNS governance routes `ExecuteGenericNervousSystemFunction` directly to this zero-cycles path: [5](#0-4) 

---

### Impact Explanation

On the Internet Computer, cycles are the native currency for paying for computation and inter-canister services. Many canister methods require cycles to be attached to the incoming call (e.g., the CMC's `deposit_cycles`, canister HTTP request methods, or any custom service that charges in cycles). Because `ExecuteGenericNervousSystemFunction` calls `env.call_canister()` with zero cycles and there is no field in the proposal payload to specify cycles, SNS governance proposals that need to call such methods will either:

1. Be rejected by the target canister (if it checks `msg_cycles_available()` and finds zero), or
2. Silently succeed at the call level but deliver no cycles to the target, causing the target to malfunction.

This permanently blocks an entire class of SNS governance actions — any proposal that needs to pay for a service in cycles — from being executable through the `ExecuteGenericNervousSystemFunction` mechanism.

---

### Likelihood Explanation

Any SNS DAO that registers a `GenericNervousSystemFunction` targeting a canister method that requires cycle payment will encounter this limitation. This is a realistic scenario: SNS DAOs may want to use governance proposals to top up canisters, pay for canister HTTP requests, or interact with cycle-priced services. The entry path is fully unprivileged — any neuron holder can submit such a proposal, observe it pass, and then observe it fail to deliver cycles. The root cause is structural (missing parameter in the trait and struct), not configuration-dependent.

---

### Recommendation

1. Add an optional `cycles_to_attach: Option<u64>` field to `ExecuteGenericNervousSystemFunction` in the protobuf definition (`rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto`).
2. Extend the `Environment::call_canister` trait method to accept an optional cycles parameter, or add a separate `call_canister_with_cycles` method.
3. In `perform_execute_generic_nervous_system_function_call`, pass the specified cycles (from the governance canister's own balance) when making the inter-canister call, using `call_with_payment128` or equivalent.
4. Add validation at proposal submission time to ensure the governance canister has sufficient cycle balance to cover the requested attachment.

---

### Proof of Concept

1. An SNS DAO registers a `GenericNervousSystemFunction` (id ≥ 1000) targeting a canister method `pay_for_service` on canister `X`, where `X` checks `msg_cycles_available() >= REQUIRED_CYCLES` and traps if insufficient.
2. A neuron holder submits an `ExecuteGenericNervousSystemFunction` proposal with `function_id` pointing to the above function.
3. The proposal passes governance voting.
4. `perform_execute_generic_nervous_system_function_call` is invoked, calling `env.call_canister(X, "pay_for_service", payload)` with zero cycles attached.
5. Canister `X` receives the call, finds `msg_cycles_available() == 0`, and rejects/traps.
6. The proposal is marked as `Failed`, and the intended action is never executed.
7. There is no way to retry with cycles attached, as neither the proposal struct nor the `call_canister` interface supports it. [6](#0-5) [7](#0-6)

### Citations

**File:** rs/sns/governance/src/canister_control.rs (L277-315)
```rust
/// Executes a generic nervous system function (i.e., a non-native SNS proposal).
pub async fn perform_execute_generic_nervous_system_function_call(
    env: &dyn Environment,
    function: NervousSystemFunction,
    call: ExecuteGenericNervousSystemFunction,
) -> Result<(), GovernanceError> {
    // Get the canister id and the method against which we execute the proposal.
    let valid_function = ValidGenericNervousSystemFunction::try_from(&function)
        .map_err(|e| GovernanceError::new_with_message(ErrorType::InvalidProposal, e))?;

    let result = env
        .call_canister(
            valid_function.target_canister_id,
            &valid_function.target_method,
            call.payload,
        )
        .await;

    // Convert result.
    match result {
        Err(err) => Err(GovernanceError::new_with_message(
            ErrorType::External,
            format!("Canister method call to execute proposal failed: {err:?}"),
        )),

        Ok(_reply) => {
            // TODO: Do something with reply. E.g. store it in the proposal,
            // and/or deserialize it so that we can detect whether there was an
            // application-level error, as opposed to a communication
            // error. Detecting application error could be done as follows:
            //
            //   candid::!Decode(&reply, Result<String, String>)
            //
            // This could then be converted into a Result<(), GovernanceError>.
            // For now, any reply is considered a success.
            Ok(())
        }
    }
}
```

**File:** rs/sns/root/src/types.rs (L26-31)
```rust
    async fn call_canister(
        &self,
        canister_id: CanisterId,
        method_name: &str,
        arg: Vec<u8>,
    ) -> Result</* reply: */ Vec<u8>, (/* error_code: */ i32, /* message: */ String)>;
```

**File:** rs/nns/integration_tests/test_canisters/unstoppable_sns_root_canister.rs (L26-33)
```rust
    async fn call_canister(
        &self,
        canister_id: CanisterId,
        method_name: &str,
        arg: Vec<u8>,
    ) -> Result<Vec<u8>, (i32, String)> {
        CanisterRuntime::call_bytes_with_cleanup(canister_id, method_name, &arg).await
    }
```

**File:** rs/sns/governance/src/gen/ic_sns_governance.pb.v1.rs (L397-409)
```rust
pub struct ExecuteGenericNervousSystemFunction {
    /// This enum value determines what canister to call and what
    /// function to call on that canister.
    ///
    /// 'function_id` must be in the range `\[1000--u64:MAX\]` as this
    /// can't be used to execute native functions.
    #[prost(uint64, tag = "1")]
    pub function_id: u64,
    /// The payload of the nervous system function's payload.
    #[prost(bytes = "vec", tag = "2")]
    #[serde(with = "serde_bytes")]
    pub payload: ::prost::alloc::vec::Vec<u8>,
}
```

**File:** rs/sns/governance/src/governance.rs (L2172-2175)
```rust
            Action::ExecuteGenericNervousSystemFunction(call) => {
                self.perform_execute_generic_nervous_system_function(call)
                    .await
            }
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L317-331)
```text
// A proposal function defining a generic proposal, i.e., a proposal
// that is not built into the standard SNS and calls a canister outside
// the SNS for execution.
// The canister and method to call are derived from the `function_id`.
message ExecuteGenericNervousSystemFunction {
  // This enum value determines what canister to call and what
  // function to call on that canister.
  //
  // 'function_id` must be in the range `[1000--u64:MAX]` as this
  // can't be used to execute native functions.
  uint64 function_id = 1;

  // The payload of the nervous system function's payload.
  bytes payload = 2;
}
```
