### Title
EVM Precompile `FatalRuntimeError` Propagation Causes Unexpected Transaction-Level Abort Instead of Call-Level Failure — (`system_hooks/src/call_hooks/precompiles.rs`)

---

### Summary

In `pure_system_function_hook_impl`, when a precompile (ecrecover, sha256, ripemd-160, identity, modexp, ecadd, ecmul, ecpairing) encounters a `FatalRuntimeError` — specifically `OutOfNativeResources` or `OutOfReturnMemory` — the error is **propagated as a `SystemError`** rather than being absorbed and converted into a graceful call-level failure. This violates EVM semantics, which require that a precompile failure only fails the specific `CALL` instruction (returning 0 in the success slot), not the entire transaction. The analog to the external report is: a function that MUST NOT abort the transaction (per EVM spec) can abort it due to ZKsync-specific resource exhaustion paths that are not handled gracefully.

---

### Finding Description

In `system_hooks/src/call_hooks/precompiles.rs`, the generic precompile dispatcher `pure_system_function_hook_impl` handles errors from precompile execution as follows:

```rust
Err(e) => match e.root_cause() {
    // Following EVM precompiles, we burn all gas on out-of-gas or invalid inputs
    RootCause::Runtime(RuntimeError::OutOfErgs(_)) | RootCause::Usage(_) => {
        resources.exhaust_ergs();
        Ok((make_error_return_state(resources), rest))  // graceful call-level failure
    }
    // Internal error means something is fatally wrong inside the hook, so we propagate it
    RootCause::Internal(e) => Err(e.clone_or_copy().into()),
    // On fatal runtime error (e.g., out of return memory or native resources) we also propagate the error
    RootCause::Runtime(e @ RuntimeError::FatalRuntimeError(_)) => {
        Err(e.clone_or_copy().into())  // ← propagates as SystemError
    }
},
``` [1](#0-0) 

`FatalRuntimeError` covers two variants:

```rust
pub enum FatalRuntimeError {
    OutOfNativeResources(Metadata),
    OutOfReturnMemory(Metadata),
}
``` [2](#0-1) 

When `pure_system_function_hook_impl` returns `Err(SystemError)`, this propagates up through the EVM interpreter call stack. In `create_frame_and_execute_transaction_payload`, a `FatalRuntimeError` is caught and converted to a **transaction-level revert with all gas exhausted**:

```rust
Err(e) => match e.root_cause() {
    RootCause::Runtime(e @ RuntimeError::FatalRuntimeError(_)) => {
        context.resources.main_resources.exhaust_ergs();
        system.finish_global_frame(Some(&main_body_rollback_handle))?;
        ExecutionResult::Revert { output: &[] }
    }
    _ => return Err(e),
},
``` [3](#0-2) 

**Concrete trigger — identity precompile with large input:**

The identity precompile charges native resources proportional to input length:

```rust
let cost_native = ID_BASE_NATIVE_COST + ID_BYTE_NATIVE_COST * (src.len() as u64);
resources.charge(&R::from_ergs_and_native(cost_ergs, ...))?;
``` [4](#0-3) 

For a 1 MB input:
- **EVM gas cost**: `15 + 3 * ceil(1_048_576 / 32)` ≈ 98,319 gas
- **Native cost**: `20 + 10 * 1_048_576` = 10,485,780 native units

The native resource budget is `gas_limit * gas_price / native_price`. At typical values (e.g., `gas_price = 1000`, `native_price = 1000`), a transaction providing 98,319 EVM gas has a native budget of only ~98,319 native units — far below the 10,485,780 required. The precompile exhausts native resources, emits `FatalRuntimeError::OutOfNativeResources`, which propagates out of `pure_system_function_hook_impl` as a `SystemError`, and aborts the entire transaction.

Additionally, `OutOfReturnMemory` is emitted by the identity precompile when the pre-allocated return buffer is too small:

```rust
dst.try_extend(src.iter().cloned())
    .map_err(|_| out_of_return_memory!())?;
``` [5](#0-4) 

This is also a `FatalRuntimeError` and follows the same propagation path.

---

### Impact Explanation

**EVM semantic mismatch / valid-execution unprovability.** Per EVM specification, a precompile call failure (including out-of-gas) must only fail the `CALL` instruction — the calling contract retains its remaining gas and can inspect the return value (0 = failure). Contracts commonly pattern-match on precompile call success/failure to implement fallback logic (e.g., try ecpairing, handle failure, continue).

In ZKsync OS, when a precompile runs out of native resources or return memory, the entire transaction is aborted with all gas consumed and all state changes reverted. This means:

1. **Contracts that handle precompile failures gracefully will instead have their entire transaction aborted** — the graceful failure path is never reached.
2. **All gas is consumed** — the user loses the full gas limit, not just the gas forwarded to the precompile.
3. **State changes from earlier in the transaction are reverted** — even operations that succeeded before the precompile call are lost.

This is a direct analog to the external report: a function that MUST NOT abort the transaction (EVM precompile call) can abort it due to an unhandled error path in the catch/error-handling logic.

---

### Likelihood Explanation

**Medium.** The trigger is reachable by any unprivileged transaction sender:

- The native resource cost of precompiles (especially identity and ecpairing) scales with input size and can far exceed the native budget derived from EVM gas.
- A user submitting a transaction with standard `eth_estimateGas`-derived gas (which does not account for native resource costs of large precompile inputs) will have insufficient native budget.
- Any contract that calls the identity precompile with large data (e.g., a data availability or blob-processing contract) is vulnerable to this abort path.
- No privileged access, governance, or oracle manipulation is required.

---

### Recommendation

In `pure_system_function_hook_impl`, handle `FatalRuntimeError::OutOfNativeResources` and `FatalRuntimeError::OutOfReturnMemory` the same way `OutOfErgs` is handled — exhaust ergs and return a graceful call-level error state — rather than propagating them as a `SystemError`:

```rust
RootCause::Runtime(RuntimeError::FatalRuntimeError(
    FatalRuntimeError::OutOfNativeResources(_) | FatalRuntimeError::OutOfReturnMemory(_)
)) => {
    resources.exhaust_ergs();
    let (_, rest) = return_vec.destruct();
    Ok((make_error_return_state(resources), rest))
}
```

This ensures that precompile resource exhaustion only fails the specific `CALL` instruction, preserving EVM semantics. The native resource cost of precompiles should also be re-examined to ensure it is proportional to their EVM gas cost, so that a transaction with sufficient EVM gas also has sufficient native budget.

---

### Proof of Concept

1. Deploy a contract that calls the identity precompile (address `0x04`) with 1 MB of calldata and checks the return value: if the call fails (returns 0), it emits an event and continues; if it succeeds, it stores the result.
2. Submit a transaction calling this contract with `gas_limit = 200_000` (sufficient EVM gas for the identity precompile) and `gas_price` such that `gas_price / native_price < 107` (insufficient native budget for 1 MB identity).
3. **Expected EVM behavior**: The identity precompile call fails (returns 0), the contract handles the failure gracefully, the transaction succeeds, and the event is emitted.
4. **Actual ZKsync OS behavior**: The identity precompile exhausts native resources → `FatalRuntimeError::OutOfNativeResources` → propagates from `pure_system_function_hook_impl` as `SystemError` → caught in `create_frame_and_execute_transaction_payload` → entire transaction reverts with all gas consumed.

The root cause is at: [6](#0-5)

### Citations

**File:** system_hooks/src/call_hooks/precompiles.rs (L87-101)
```rust
        Err(e) => match e.root_cause() {
            // Following EVM precompiles, we burn all gas on out-of-gas or invalid inputs
            RootCause::Runtime(RuntimeError::OutOfErgs(_)) | RootCause::Usage(_) => {
                system_log!(system, "Out of gas during system hook\nError:{e:?}");
                resources.exhaust_ergs();
                let (_, rest) = return_vec.destruct();
                Ok((make_error_return_state(resources), rest))
            }
            // Internal error means something is fatally wrong inside the hook, so we propagate it
            RootCause::Internal(e) => Err(e.clone_or_copy().into()),
            // On fatal runtime error (e.g., out of return memory or native resources) we also propagate the error
            RootCause::Runtime(e @ RuntimeError::FatalRuntimeError(_)) => {
                Err(e.clone_or_copy().into())
            }
        },
```

**File:** system_hooks/src/call_hooks/precompiles.rs (L123-129)
```rust
            let cost_ergs =
                ID_STATIC_COST_ERGS + ID_WORD_COST_ERGS.times((src.len() as u64).div_ceil(32));
            let cost_native = ID_BASE_NATIVE_COST + ID_BYTE_NATIVE_COST * (src.len() as u64);
            resources.charge(&R::from_ergs_and_native(
                cost_ergs,
                <R::Native as zk_ee::system::Computational>::from_computational(cost_native),
            ))?;
```

**File:** system_hooks/src/call_hooks/precompiles.rs (L130-131)
```rust
            dst.try_extend(src.iter().cloned())
                .map_err(|_| out_of_return_memory!())?;
```

**File:** zk_ee/src/system/errors/runtime.rs (L12-15)
```rust
pub enum FatalRuntimeError {
    OutOfNativeResources(Metadata),
    OutOfReturnMemory(Metadata),
}
```

**File:** basic_bootloader/src/bootloader/transaction_flow/ethereum/mod.rs (L441-453)
```rust
            Err(e) => match e.root_cause() {
                RootCause::Runtime(e @ RuntimeError::FatalRuntimeError(_)) => {
                    system_log!(
                        system,
                        "Transaction ran out of native resources or memory: {e:?}\n"
                    );
                    context.resources.main_resources.exhaust_ergs();
                    system.finish_global_frame(Some(&main_body_rollback_handle))?;

                    ExecutionResult::Revert { output: &[] }
                }
                _ => return Err(e),
            },
```
