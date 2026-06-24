### Title
Unchecked Application-Level Return Value in SNS Generic Nervous System Function Execution Marks Failed Proposals as Executed - (File: rs/sns/governance/src/canister_control.rs)

### Summary

`perform_execute_generic_nervous_system_function_call` in the SNS governance canister treats any transport-level reply from the target canister as a successful proposal execution, without inspecting the reply bytes for application-level errors. A target canister that returns an encoded `Err(...)` value (rather than reverting/rejecting) will cause the SNS proposal to be permanently marked as "executed" even though the intended action had no effect.

### Finding Description

In `rs/sns/governance/src/canister_control.rs`, the function `perform_execute_generic_nervous_system_function_call` dispatches a canister call to the registered target canister and method for an `ExecuteGenericNervousSystemFunction` proposal: [1](#0-0) 

The `Ok(_reply)` arm discards the reply bytes entirely and returns `Ok(())`, with a `TODO` comment explicitly acknowledging that application-level errors in the reply are not detected. The contrast with the **validator** path is instructive: `perform_execute_generic_nervous_system_function_validate_and_render_call` does decode the reply as `Result<String, String>` and propagates errors: [2](#0-1) 

The execution path does not apply the same discipline.

This result propagates up through `perform_execute_generic_nervous_system_function`: [3](#0-2) 

And then through `perform_action`: [4](#0-3) 

The final `Ok(())` is passed to `set_proposal_execution_status`, which permanently marks the proposal as executed: [5](#0-4) 

### Impact Explanation

An `ExecuteGenericNervousSystemFunction` SNS proposal targeting a canister method that uses `Result<_, _>` return types (a common Candid pattern) and returns an `Err(...)` variant will be permanently marked as `executed_timestamp_seconds > 0` even though the intended action (e.g., a parameter change, treasury operation, or dapp configuration update) did not take effect. The proposal is consumed and cannot be re-executed. SNS governance participants and monitoring tools will observe the proposal as successfully executed, creating a false belief that the system state was updated.

**Vulnerability type:** Governance authorization bug / silent failure in proposal execution.

### Likelihood Explanation

Any SNS neuron holder can submit an `ExecuteGenericNervousSystemFunction` proposal. The target canister and method are registered by prior governance action. Many real-world canister methods return `Result<_, _>` and may return `Err(...)` due to precondition failures, misconfiguration, or transient state. The scenario is realistic and reachable by any unprivileged SNS governance participant without any privileged access.

### Recommendation

**Short term:** Document that any reply from the target canister — including encoded application-level errors — is treated as a successful execution. SNS operators should ensure target canister methods reject (trap/panic) rather than return `Err(...)` to signal failure.

**Long term:** Decode the reply bytes and check for application-level errors before marking the proposal as executed. The code already contains a `TODO` with the exact approach:

```rust
Ok(reply) => {
    match candid::Decode!(&reply, Result<String, String>) {
        Ok(Err(e)) => Err(GovernanceError::new_with_message(ErrorType::External, e)),
        _ => Ok(()),
    }
}
```

A convention (e.g., requiring target methods to return `Result<_, String>`) should be established and enforced at proposal registration time.

### Proof of Concept

1. Register a `NervousSystemFunction` whose `target_method` returns `Result<(), String>` and returns `Err("precondition failed".to_string())` under certain conditions.
2. Submit and adopt an `ExecuteGenericNervousSystemFunction` proposal targeting that method when the error condition is active.
3. Observe that `perform_execute_generic_nervous_system_function_call` receives `Ok(encoded_err_bytes)` from `env.call_canister`.
4. The `Ok(_reply)` arm at line 302 discards the bytes and returns `Ok(())`.
5. `set_proposal_execution_status` sets `proposal.executed_timestamp_seconds = self.env.now()`.
6. The proposal is permanently marked as executed; the intended action had no effect; no retry is possible. [6](#0-5) [7](#0-6)

### Citations

**File:** rs/sns/governance/src/canister_control.rs (L262-274)
```rust
        Ok(reply) => {
            let result = Decode!(&reply, Result<String,String>);
            match result {
                Err(e) => Err(format!(
                    "Error decoding reply from proposal payload validate and render call: {e}"
                )),
                Ok(value) => match value {
                    Err(e) => Err(format!("Invalid proposal: {e}")),
                    Ok(rendering) => Ok(rendering),
                },
            }
        }
    }
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

**File:** rs/sns/governance/src/governance.rs (L1716-1731)
```rust
    pub fn set_proposal_execution_status(&mut self, pid: u64, result: Result<(), GovernanceError>) {
        match self.proto.proposals.get_mut(&pid) {
            Some(proposal) => {
                // The proposal has to be adopted before it is executed.
                assert_eq!(proposal.status(), ProposalDecisionStatus::Adopted);
                match result {
                    Ok(_) => {
                        log!(INFO, "Execution of proposal: {} succeeded.", pid);
                        // The proposal was executed 'now'.
                        proposal.executed_timestamp_seconds = self.env.now();
                        // If the proposal was executed it has not failed,
                        // thus we set the failed_timestamp_seconds to zero
                        // (it should already be zero, but let's be defensive).
                        proposal.failed_timestamp_seconds = 0;
                        proposal.failure_reason = None;
                    }
```

**File:** rs/sns/governance/src/governance.rs (L2172-2175)
```rust
            Action::ExecuteGenericNervousSystemFunction(call) => {
                self.perform_execute_generic_nervous_system_function(call)
                    .await
            }
```

**File:** rs/sns/governance/src/governance.rs (L2531-2555)
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
```
