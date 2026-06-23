### Title
Strict Cycles Balance Equality Check in DTS Resume Enables Denial-of-Service Against Long-Running Canister Executions - (File: `rs/execution_environment/src/execution/call_or_task.rs`, `rs/execution_environment/src/execution/response.rs`)

---

### Summary

The Deterministic Time Slicing (DTS) execution resumption logic uses a strict equality (`!=`) check to compare a canister's cycles balance between DTS slices. Because any canister on the IC can deposit cycles to any other canister via the `deposit_cycles` management canister call, an unprivileged attacker can deposit a trivial amount of cycles to a victim canister between DTS slices, causing the strict balance check to fail and aborting the victim's long-running execution. This is a direct analog to the Tensor tlock "Overly Strict Checks On Account Balance" vulnerability.

---

### Finding Description

When a canister executes a long-running update call, response callback, or replicated query under DTS, execution is split across multiple rounds. At the start of each slice, the execution engine records `initial_cycles_balance` and stores it in a `PausedCallOrTaskHelper` or `PausedResponseHelper`. When resuming in a subsequent round, the engine re-reads the canister's current balance and compares it with the stored value using strict inequality:

**`rs/execution_environment/src/execution/call_or_task.rs`**, `CallOrTaskHelper::resume()`:

```rust
if helper.initial_cycles_balance != paused.initial_cycles_balance {
    let msg = "Mismatch in cycles balance when resuming an update call".to_string();
    let err = HypervisorError::WasmEngineError(FailedToApplySystemChanges(msg));
    return Err(err.into_user_error(&clean_canister.canister_id()));
}
``` [1](#0-0) 

**`rs/execution_environment/src/execution/response.rs`**, `ResponseHelper::resume()`:

```rust
if helper.initial_cycles_balance != paused.initial_cycles_balance {
    let msg = "Mismatch in cycles balance when resuming a response call".to_string();
    let err = HypervisorError::WasmEngineError(FailedToApplySystemChanges(msg));
    return Err((helper, err));
}
``` [2](#0-1) 

The `initial_cycles_balance` is captured at the start of each slice via `canister.system_state.balance()`: [3](#0-2) 

The check is intended to detect if cycles were *removed* from the canister between slices (which would affect execution correctness). However, it also fires if cycles are *added* — which is harmless to execution correctness but causes the execution to abort with `CanisterWasmEngineError`.

Any canister on the IC can deposit cycles to any other canister using the `deposit_cycles` management canister call. This is standard, permissionless IC behavior. An attacker canister can therefore deposit 1 cycle to a victim canister between DTS slices, causing the strict equality check to fail and aborting the victim's execution.

The existing test `dts_replicated_execution_resume_fails_due_to_cycles_change` explicitly confirms this behavior — adding cycles to a paused canister causes the resume to fail: [4](#0-3) 

---

### Impact Explanation

An unprivileged attacker canister can permanently deny service to any canister that relies on DTS for long-running executions (update calls, response callbacks, install_code). Every time the victim canister starts a multi-slice execution, the attacker deposits a trivial amount of cycles between slices, causing the execution to abort with `CanisterWasmEngineError`. The victim's ingress message or inter-canister call fails, and the attacker can repeat this indefinitely. Canisters performing large Wasm installs, heavy computation, or complex response callbacks are all affected. The cost to the attacker is only the deposited cycles (1 cycle per attack), making this economically trivial.

---

### Likelihood Explanation

The attack is straightforward: the attacker deploys a canister, monitors for a target canister entering DTS (observable via the public `canister_status` API or by timing), and calls `deposit_cycles` on the management canister targeting the victim. No privileged access, no key material, and no consensus-level corruption is required. The `deposit_cycles` call is a standard, documented IC management canister method available to all canisters. The attack is repeatable at negligible cost.

---

### Recommendation

Replace the strict equality check with a one-sided check that only fails when the balance has *decreased* (indicating cycles were removed, which genuinely affects execution correctness). An increase in balance (cycles deposited by a third party) is harmless and should be tolerated:

```rust
// In call_or_task.rs and response.rs:
// Before:
if helper.initial_cycles_balance != paused.initial_cycles_balance { ... }

// After:
if helper.initial_cycles_balance < paused.initial_cycles_balance { ... }
```

This mirrors the remediation in the original Tensor tlock report: replace strict equality with a directional comparison that only rejects the harmful case (balance decrease), not the harmless case (balance increase).

---

### Proof of Concept

1. Victim canister `V` receives a large ingress update call that requires multiple DTS slices to complete.
2. After the first slice executes, `V` is in `NextExecution::ContinueLong` state with `initial_cycles_balance` stored in `PausedCallOrTaskHelper`.
3. Attacker canister `A` calls `deposit_cycles` on the IC management canister, depositing 1 cycle to `V`.
4. `V`'s `system_state.balance()` is now `paused.initial_cycles_balance + 1`.
5. On the next round, `CallOrTaskHelper::resume()` computes `helper.initial_cycles_balance = paused.initial_cycles_balance + 1`, which `!= paused.initial_cycles_balance`.
6. The check at line 438 of `call_or_task.rs` fires, returning `CanisterWasmEngineError` / `FailedToApplySystemChanges`.
7. The victim's ingress message fails with `ErrorCode::CanisterWasmEngineError`.
8. Attacker repeats for every subsequent attempt by `V` to complete a DTS execution. [5](#0-4) [6](#0-5)

### Citations

**File:** rs/execution_environment/src/execution/call_or_task.rs (L397-397)
```rust
        let initial_cycles_balance = canister.system_state.balance();
```

**File:** rs/execution_environment/src/execution/call_or_task.rs (L431-452)
```rust
    fn resume(
        clean_canister: &CanisterState,
        original: &OriginalContext,
        paused: PausedCallOrTaskHelper,
        deallocation_sender: &DeallocationSender,
    ) -> Result<Self, UserError> {
        let helper = Self::new(clean_canister, original, deallocation_sender)?;
        if helper.initial_cycles_balance != paused.initial_cycles_balance {
            let msg = match original.call_or_task {
                CanisterCallOrTask::Update(_) => {
                    "Mismatch in cycles balance when resuming an update call".to_string()
                }
                CanisterCallOrTask::Query(_) => {
                    "Mismatch in cycles balance when resuming a replicated query".to_string()
                }
                CanisterCallOrTask::Task(_) => {
                    "Mismatch in cycles balance when resuming a canister task".to_string()
                }
            };
            let err = HypervisorError::WasmEngineError(FailedToApplySystemChanges(msg));
            return Err(err.into_user_error(&clean_canister.canister_id()));
        }
```

**File:** rs/execution_environment/src/execution/response.rs (L321-373)
```rust
    fn resume(
        paused: PausedResponseHelper,
        clean_canister: &CanisterState,
        original: &OriginalContext,
        round: &RoundContext,
        round_limits: &mut RoundLimits,
        deallocation_sender: &DeallocationSender,
    ) -> Result<ResponseHelper, (ResponseHelper, HypervisorError)> {
        // We expect the function call to succeed because the call context and
        // the callback have been checked in `execute_response()`.
        // Note that we cannot return an error here because the cleanup callback
        // cannot be invoked without a valid call context and a callback.
        let (call_context, _) = common::get_call_context(
            clean_canister,
            &original.callback,
            round.log,
            round.counters.unexpected_response_error,
        )
        .expect("Failed to resume DTS response: get call context and callback");

        let mut helper = Self {
            canister: clean_canister.clone(),
            refund_for_sent_cycles: paused.refund_for_sent_cycles,
            prepayment_for_response_transmission: paused.prepayment_for_response_transmission,
            prepayment_for_call_transmission: paused.prepayment_for_call_transmission,
            refund_for_response_transmission: paused.refund_for_response_transmission,
            initial_cycles_balance: clean_canister.system_state.balance(),
            response_sender: paused.response_sender,
            applied_subnet_memory_reservation: NumBytes::new(0),
            deallocation_sender: deallocation_sender.clone(),
        };

        helper.apply_subnet_memory_reservation(round_limits);

        helper.apply_initial_refunds();

        // This validation succeeded in `execute_response()` and we expect it to
        // succeed here too.
        // Note that we cannot return an error here because the cleanup callback
        // cannot be invoked without a valid call context and a callback.
        helper = helper
            .validate(&call_context, original, round, round_limits)
            .expect("Failed to resume DTS response: validation");

        // The cycles balance of the clean canister must not change during the
        // DTS execution.
        if helper.initial_cycles_balance != paused.initial_cycles_balance {
            let msg = "Mismatch in cycles balance when resuming a response call".to_string();
            let err = HypervisorError::WasmEngineError(FailedToApplySystemChanges(msg));
            return Err((helper, err));
        }
        Ok(helper)
    }
```

**File:** rs/execution_environment/src/execution/call_or_task/tests.rs (L724-781)
```rust
fn dts_replicated_execution_resume_fails_due_to_cycles_change() {
    with_update_and_replicated_query(|method| {
        // Test steps:
        // 1. Canister A starts running the update|query method.
        // 2. While canister A is paused, we change its cycles balance.
        // 3. The update|query method resumes, detects the cycles balance mismatch, and
        //    fails.
        let instruction_limit = 1_000_000;
        let mut test = ExecutionTestBuilder::new()
            .with_instruction_limit(instruction_limit)
            .with_slice_instruction_limit(10_000)
            .with_manual_execution()
            .build();

        let a_id = test.universal_canister().unwrap();

        let a = wasm()
            .stable64_grow(1)
            .stable64_fill(0, 0, 10_000)
            .stable64_fill(0, 0, 10_000)
            .build();

        let (ingress_id, _) = test.ingress_raw(a_id, method, a);

        test.execute_slice(a_id);
        assert_eq!(
            test.canister_state(a_id).next_execution(),
            NextExecution::ContinueLong,
        );

        // Change the cycles balance of the clean canister.
        let balance = test.canister_state(a_id).system_state.balance();
        test.canister_state_mut(a_id)
            .system_state
            .add_cycles(balance + Cycles::new(1));

        test.execute_slice(a_id);

        assert_eq!(
            test.canister_state(a_id).next_execution(),
            NextExecution::None,
        );

        let err = check_ingress_status(test.ingress_status(&ingress_id)).unwrap_err();
        let message = if method == "update" {
            "an update call"
        } else {
            "a replicated query"
        };
        err.assert_contains(
            ErrorCode::CanisterWasmEngineError,
            &format!(
                "Error from Canister {a_id}: Canister encountered a Wasm engine error: \
             Failed to apply system changes: Mismatch in cycles \
             balance when resuming {message}"
            ),
        );
    });
```
