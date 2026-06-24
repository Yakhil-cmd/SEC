### Title
Unchecked Application-Level Return Value in SNS Governance Generic Proposal Execution Marks Failed Proposals as Executed - (`File: rs/sns/governance/src/canister_control.rs`)

---

### Summary

`perform_execute_generic_nervous_system_function_call` in the SNS governance canister makes an inter-canister call to a target canister whose documented method signature is `Result<(), String>`. The function correctly distinguishes a transport-level reject (call fails entirely) from a successful reply, but it **never decodes the reply bytes** to check whether the target returned `Ok(())` or `Err(String)`. Any reply — including an application-level `Err(String)` — is unconditionally treated as `Ok(())`, causing the proposal to be permanently marked as **executed/succeeded** even when the target canister explicitly reported failure.

---

### Finding Description

The SNS governance proto documents the required target method signature:

> `<method_name>(proposal_data: ProposalData) -> Result<(), String>` [1](#0-0) 

The execution function in `canister_control.rs` calls the target canister and then matches only on transport-level success vs. failure:

```rust
Ok(_reply) => {
    // TODO: Do something with reply. E.g. store it in the proposal,
    // and/or deserialize it so that we can detect whether there was an
    // application-level error, as opposed to a communication error.
    // For now, any reply is considered a success.
    Ok(())
}
``` [2](#0-1) 

The `_reply` bytes are discarded without decoding. If the target canister returns `Err("some failure reason")` encoded as Candid, the governance canister receives a valid reply (transport-level `Ok`), ignores the encoded error, and returns `Ok(())` to the caller.

This result propagates up through `perform_execute_generic_nervous_system_function` → `perform_action` → `set_proposal_execution_status`: [3](#0-2) [4](#0-3) 

`set_proposal_execution_status` then sets `executed_timestamp_seconds` to the current time, permanently marking the proposal as successfully executed. [5](#0-4) 

---

### Impact Explanation

1. **Proposal permanently consumed on application failure.** Once a generic nervous system function proposal is executed and the target canister returns `Err(String)`, the proposal is irrevocably marked as `Executed`. The intended on-chain action (e.g., a treasury operation, a dapp configuration change, a parameter update) was never actually performed, but governance believes it was. A new proposal must be submitted and voted on from scratch.

2. **Misleading governance state.** Any observer, dapp, or downstream canister querying proposal status via `get_proposal` will see `executed_timestamp_seconds > 0` and `failed_timestamp_seconds == 0`, indicating success. Systems that gate further actions on a proposal's execution status will proceed incorrectly.

3. **No recourse without re-queuing.** Unlike the NNS governance path (which properly propagates `Err` from the target canister back to `set_proposal_execution_status` as a failure), the SNS path silently swallows application-level errors, leaving no audit trail of the actual failure.

---

### Likelihood Explanation

The `ExecuteGenericNervousSystemFunction` action is the primary extensibility mechanism for every deployed SNS. Any SNS whose registered target canister method can return `Err(String)` — which is the documented and expected interface — is affected. This is reachable by any SNS token holder with sufficient voting power to pass a proposal, which is an unprivileged role by design. The likelihood is **high** for any SNS that uses generic nervous system functions with fallible target methods.

---

### Recommendation

Decode the reply bytes and propagate application-level errors as governance failures:

```rust
Ok(reply) => {
    match candid::Decode!(&reply, Result<(), String>) {
        Ok(Ok(())) => Ok(()),
        Ok(Err(app_err)) => Err(GovernanceError::new_with_message(
            ErrorType::External,
            format!("Target canister returned application error: {app_err}"),
        )),
        Err(decode_err) => Err(GovernanceError::new_with_message(
            ErrorType::External,
            format!("Failed to decode reply from target canister: {decode_err}"),
        )),
    }
}
``` [2](#0-1) 

---

### Proof of Concept

1. Register a generic nervous system function whose target method is implemented as:
   ```rust
   #[update]
   fn my_action(_payload: Vec<u8>) -> Result<(), String> {
       Err("intentional failure".to_string())
   }
   ```
2. Submit and pass an `ExecuteGenericNervousSystemFunction` proposal targeting this method.
3. After execution, query the proposal via `get_proposal`. Observe `executed_timestamp_seconds > 0` and `failed_timestamp_seconds == 0`.
4. The target canister's action was never performed, but governance permanently records the proposal as successfully executed.
5. Submitting a second identical proposal is the only recourse, requiring a full new voting cycle. [6](#0-5) [7](#0-6)

### Citations

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L287-289)
```text
    // The signature of the method must be equivalent to the following:
    // <method_name>(proposal_data: ProposalData) -> Result<(), String>.
    optional string target_method_name = 3;
```

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

**File:** rs/sns/governance/src/governance.rs (L2172-2175)
```rust
            Action::ExecuteGenericNervousSystemFunction(call) => {
                self.perform_execute_generic_nervous_system_function(call)
                    .await
            }
```

**File:** rs/sns/governance/src/governance.rs (L2240-2243)
```rust
        };

        self.set_proposal_execution_status(proposal_id, result);
    }
```

**File:** rs/sns/governance/src/governance.rs (L2530-2556)
```rust
    /// Executes a (non-native) nervous system function as a result of an adopted proposal.
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

**File:** rs/nns/governance/src/governance.rs (L3234-3234)
```rust
        proposal_data.executed_timestamp_seconds = now_timestamp_seconds;
```
