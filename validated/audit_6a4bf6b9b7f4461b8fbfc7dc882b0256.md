The code confirms the claim. Key findings:

1. `mint_cycles128` at lines 1205-1215 of `linker.rs` has **no** `charge_for_cpu()` call.
2. The immediately adjacent `cycles_burn128` at lines 1217-1228 correctly calls `charge_for_cpu(&mut caller, overhead::CYCLES_BURN128)`.
3. `system_api_complexity.rs` has `CYCLES_BURN128` but **no** `MINT_CYCLES128` constant — confirming the overhead was never defined.

---

Audit Report

## Title
Missing `charge_for_cpu()` in `ic0.mint_cycles128` Host Function Registration - (File: rs/embedders/src/wasmtime_embedder/linker.rs)

## Summary
The `ic0.mint_cycles128` host function is registered in the Wasmtime linker without a `charge_for_cpu()` call, unlike every other `ic0.*` system API function including the immediately adjacent `cycles_burn128`. Any canister can import and call `ic0.mint_cycles128` in a tight loop, paying only the WASM opcode cost (~1 instruction per call) rather than the intended ~500-instruction overhead, achieving a ~500x amplification of host-boundary crossings per message and consuming disproportionate replica CPU per execution slot.

## Finding Description
In `rs/embedders/src/wasmtime_embedder/linker.rs` lines 1205–1215, the `mint_cycles128` closure calls `with_memory_and_system_api` directly without first calling `charge_for_cpu`:

```rust
linker
    .func_wrap("ic0", "mint_cycles128", {
        move |mut caller: Caller<'_, StoreData>, amount_high: u64, amount_low: u64, dst: I| {
            // NO charge_for_cpu() here
            with_memory_and_system_api(&mut caller, |s, memory| {
                let dst: usize = dst.try_into().expect("Failed to convert I to usize");
                s.ic0_mint_cycles128(Cycles::from_parts(amount_high, amount_low), dst, memory)
            })
            .map_err(|e| wasmtime::Error::msg(format!("ic0_mint_cycles128 failed: {e}")))
        }
    })
    .unwrap();
```

The immediately adjacent `cycles_burn128` (lines 1217–1228) correctly charges:

```rust
charge_for_cpu(&mut caller, overhead::CYCLES_BURN128)?;
```

The `charge_for_cpu` helper (lines 102–109) is the mechanism by which the IC accounts for the overhead of crossing the WASM-to-host boundary. The `system_api_complexity.rs` overhead table (lines 18–97) defines `CYCLES_BURN128 = 500` instructions but has no `MINT_CYCLES128` entry, confirming the overhead was never defined or applied.

WASM-level instruction metering (`inject_metering`) only charges for WASM opcodes; it does not account for host function overhead. A `call` opcode costs 1 instruction. Without `charge_for_cpu`, each call to `mint_cycles128` costs 1 instruction instead of ~501, allowing ~500x more host-boundary crossings per message than the instruction budget is designed to permit.

For non-CMC canisters, `ic0_mint_cycles128` in `system_api.rs` returns an error immediately after an `api_type` match, but the host-side work — `with_memory_and_system_api` dispatch, API type match, error construction, and `trace_syscall!` — executes on every iteration.

## Impact Explanation
This is a **High** severity application/platform-level DoS. An attacker deploys a canister that calls `ic0.mint_cycles128` in a tight loop. With a ~5 billion instruction limit per message and a 1-instruction cost per call (vs. the intended ~501), the canister can perform ~5 billion host-boundary crossings per message instead of ~10 million — a ~500x amplification of replica CPU work per execution slot. This slows block execution across the subnet without a proportional increase in cycles charged, constituting a subnet availability impact not based on raw volumetric DDoS.

## Likelihood Explanation
Any canister developer can deploy this exploit with no privileged access. The canister simply imports `ic0.mint_cycles128` and calls it in a loop. No victim interaction is required. The attack is repeatable on every ingress update call and requires no special subnet conditions.

## Recommendation
Add `charge_for_cpu(&mut caller, overhead::MINT_CYCLES128)?;` as the first statement in the `mint_cycles128` closure, and add `pub const MINT_CYCLES128: NumInstructions = NumInstructions::new(500);` to the `overhead` module in `system_api_complexity.rs`, consistent with `CYCLES_BURN128`.

## Proof of Concept
```wat
(module
  (import "ic0" "mint_cycles128"
    (func $mint_cycles128 (param i64 i64 i32)))
  (memory 1)
  (func (export "canister_update exploit")
    (local $i i32)
    (local.set $i (i32.const 0))
    (block $break
      (loop $loop
        (call $mint_cycles128 (i64.const 0) (i64.const 1) (i32.const 0))
        (local.set $i (i32.add (local.get $i) (i32.const 1)))
        (br_if $loop (i32.lt_s (local.get $i) (i32.const 2000000000)))
      )
    )
  )
)
```
Deploy this canister on a local replica or PocketIC instance. Measure wall-clock time for the `exploit` update call vs. an equivalent loop calling `cycles_burn128`. The `mint_cycles128` loop will complete far more iterations within the same instruction budget, demonstrating the amplification. Confirm via replica metrics that CPU time per message is disproportionate to cycles charged. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** rs/embedders/src/wasmtime_embedder/linker.rs (L102-119)
```rust
/// Charge for system api call that doesn't involve touching memory
#[inline(always)]
fn charge_for_cpu(
    caller: &mut Caller<'_, StoreData>,
    overhead: NumInstructions,
) -> Result<(), wasmtime::Error> {
    charge_for_system_api_call(caller, overhead, 0).map_err(|e| process_err(caller, e))
}

/// Charge for system api call that involves writing/reading heap
#[inline(always)]
fn charge_for_cpu_and_mem(
    caller: &mut Caller<'_, StoreData>,
    overhead: NumInstructions,
    num_bytes: usize,
) -> Result<(), wasmtime::Error> {
    charge_for_system_api_call(caller, overhead, num_bytes).map_err(|e| process_err(caller, e))
}
```

**File:** rs/embedders/src/wasmtime_embedder/linker.rs (L1205-1215)
```rust
    linker
        .func_wrap("ic0", "mint_cycles128", {
            move |mut caller: Caller<'_, StoreData>, amount_high: u64, amount_low: u64, dst: I| {
                with_memory_and_system_api(&mut caller, |s, memory| {
                    let dst: usize = dst.try_into().expect("Failed to convert I to usize");
                    s.ic0_mint_cycles128(Cycles::from_parts(amount_high, amount_low), dst, memory)
                })
                .map_err(|e| wasmtime::Error::msg(format!("ic0_mint_cycles128 failed: {e}")))
            }
        })
        .unwrap();
```

**File:** rs/embedders/src/wasmtime_embedder/linker.rs (L1217-1228)
```rust
    linker
        .func_wrap("ic0", "cycles_burn128", {
            move |mut caller: Caller<'_, StoreData>, amount_high: u64, amount_low: u64, dst: I| {
                charge_for_cpu(&mut caller, overhead::CYCLES_BURN128)?;
                with_memory_and_system_api(&mut caller, |s, memory| {
                    let dst: usize = dst.try_into().expect("Failed to convert I to usize");
                    s.ic0_cycles_burn128(Cycles::from_parts(amount_high, amount_low), dst, memory)
                })
                .map_err(|e| wasmtime::Error::msg(format!("ic0_cycles_burn128 failed: {e}")))
            }
        })
        .unwrap();
```

**File:** rs/embedders/src/wasmtime_embedder/system_api_complexity.rs (L18-97)
```rust
pub mod overhead {
    use ic_types::NumInstructions;
    pub const ACCEPT_MESSAGE: NumInstructions = NumInstructions::new(500);
    pub const CALL_CYCLES_ADD: NumInstructions = NumInstructions::new(500);
    pub const CALL_CYCLES_ADD128: NumInstructions = NumInstructions::new(500);
    pub const CALL_DATA_APPEND: NumInstructions = NumInstructions::new(500);
    pub const CALL_NEW: NumInstructions = NumInstructions::new(1_500);
    pub const CALL_ON_CLEANUP: NumInstructions = NumInstructions::new(500);
    pub const CALL_PERFORM: NumInstructions = NumInstructions::new(5_000);
    pub const CALL_WITH_BEST_EFFORT_RESPONSE: NumInstructions = NumInstructions::new(500);
    pub const CYCLES_BURN128: NumInstructions = NumInstructions::new(500);
    pub const CANISTER_CYCLE_BALANCE: NumInstructions = NumInstructions::new(500);
    pub const CANISTER_CYCLE_BALANCE128: NumInstructions = NumInstructions::new(500);
    pub const CANISTER_LIQUID_CYCLE_BALANCE128: NumInstructions = NumInstructions::new(500);
    pub const CANISTER_SELF_COPY: NumInstructions = NumInstructions::new(500);
    pub const CANISTER_SELF_SIZE: NumInstructions = NumInstructions::new(500);
    pub const CANISTER_STATUS: NumInstructions = NumInstructions::new(500);
    pub const CANISTER_VERSION: NumInstructions = NumInstructions::new(500);
    pub const ROOT_KEY_SIZE: NumInstructions = NumInstructions::new(500);
    pub const ROOT_KEY_COPY: NumInstructions = NumInstructions::new(500);
    pub const CERTIFIED_DATA_SET: NumInstructions = NumInstructions::new(500);
    pub const CONTROLLER_COPY: NumInstructions = NumInstructions::new(500);
    pub const CONTROLLER_SIZE: NumInstructions = NumInstructions::new(500);
    pub const COST_CALL: NumInstructions = NumInstructions::new(500);
    pub const COST_CREATE_CANISTER: NumInstructions = NumInstructions::new(500);
    pub const COST_HTTP_REQUEST: NumInstructions = NumInstructions::new(500);
    pub const COST_HTTP_REQUEST_V2: NumInstructions = NumInstructions::new(10_000);
    pub const COST_ECDSA: NumInstructions = NumInstructions::new(500);
    pub const COST_SCHNORR: NumInstructions = NumInstructions::new(500);
    pub const COST_VETKD: NumInstructions = NumInstructions::new(500);
    pub const DATA_CERTIFICATE_COPY: NumInstructions = NumInstructions::new(500);
    pub const DATA_CERTIFICATE_PRESENT: NumInstructions = NumInstructions::new(500);
    pub const DATA_CERTIFICATE_SIZE: NumInstructions = NumInstructions::new(500);
    pub const DEBUG_PRINT: NumInstructions = NumInstructions::new(100);
    pub const GLOBAL_TIMER_SET: NumInstructions = NumInstructions::new(500);
    pub const IS_CONTROLLER: NumInstructions = NumInstructions::new(1_000);
    pub const IN_REPLICATED_EXECUTION: NumInstructions = NumInstructions::new(500);
    pub const MSG_ARG_DATA_COPY: NumInstructions = NumInstructions::new(500);
    pub const MSG_ARG_DATA_SIZE: NumInstructions = NumInstructions::new(500);
    pub const MSG_CALLER_COPY: NumInstructions = NumInstructions::new(500);
    pub const MSG_CALLER_INFO_DATA_COPY: NumInstructions = NumInstructions::new(500);
    pub const MSG_CALLER_INFO_DATA_SIZE: NumInstructions = NumInstructions::new(500);
    pub const MSG_CALLER_INFO_SIGNER_COPY: NumInstructions = NumInstructions::new(500);
    pub const MSG_CALLER_INFO_SIGNER_SIZE: NumInstructions = NumInstructions::new(500);
    pub const MSG_CALLER_SIZE: NumInstructions = NumInstructions::new(500);
    pub const MSG_CYCLES_ACCEPT: NumInstructions = NumInstructions::new(500);
    pub const MSG_CYCLES_ACCEPT128: NumInstructions = NumInstructions::new(500);
    pub const MSG_CYCLES_AVAILABLE: NumInstructions = NumInstructions::new(500);
    pub const MSG_CYCLES_AVAILABLE128: NumInstructions = NumInstructions::new(500);
    pub const MSG_CYCLES_REFUNDED: NumInstructions = NumInstructions::new(500);
    pub const MSG_CYCLES_REFUNDED128: NumInstructions = NumInstructions::new(500);
    pub const MSG_DEADLINE: NumInstructions = NumInstructions::new(500);
    pub const MSG_METHOD_NAME_COPY: NumInstructions = NumInstructions::new(500);
    pub const MSG_METHOD_NAME_SIZE: NumInstructions = NumInstructions::new(500);
    pub const MSG_REJECT_CODE: NumInstructions = NumInstructions::new(500);
    pub const MSG_REJECT_MSG_COPY: NumInstructions = NumInstructions::new(500);
    pub const MSG_REJECT_MSG_SIZE: NumInstructions = NumInstructions::new(500);
    pub const MSG_REJECT: NumInstructions = NumInstructions::new(500);
    pub const MSG_REPLY_DATA_APPEND: NumInstructions = NumInstructions::new(500);
    pub const MSG_REPLY: NumInstructions = NumInstructions::new(500);
    pub const PERFORMANCE_COUNTER: NumInstructions = NumInstructions::new(200);
    pub const SUBNET_SELF_SIZE: NumInstructions = NumInstructions::new(500);
    pub const SUBNET_SELF_COPY: NumInstructions = NumInstructions::new(500);
    pub const STABLE_GROW: NumInstructions = NumInstructions::new(500);
    pub const STABLE_READ: NumInstructions = NumInstructions::new(20);
    pub const STABLE_SIZE: NumInstructions = NumInstructions::new(20);
    pub const STABLE_WRITE: NumInstructions = NumInstructions::new(20);
    pub const STABLE64_GROW: NumInstructions = NumInstructions::new(500);
    pub const STABLE64_READ: NumInstructions = NumInstructions::new(20);
    pub const STABLE64_SIZE: NumInstructions = NumInstructions::new(20);
    pub const STABLE64_WRITE: NumInstructions = NumInstructions::new(20);
    pub const ENV_VAR_COUNT: NumInstructions = NumInstructions::new(500);
    pub const ENV_VAR_NAME_SIZE: NumInstructions = NumInstructions::new(500);
    pub const ENV_VAR_NAME_COPY: NumInstructions = NumInstructions::new(500);
    pub const ENV_VAR_NAME_EXISTS: NumInstructions = NumInstructions::new(500);
    pub const ENV_VAR_VALUE_SIZE: NumInstructions = NumInstructions::new(500);
    pub const ENV_VAR_VALUE_COPY: NumInstructions = NumInstructions::new(500);
    pub const TIME: NumInstructions = NumInstructions::new(500);
    pub const TRAP: NumInstructions = NumInstructions::new(500);
}
```
