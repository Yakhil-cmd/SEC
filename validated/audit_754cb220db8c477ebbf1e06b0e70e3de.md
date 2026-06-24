Audit Report

## Title
`ic0.cycles_burn128` Ignores Pending `ingress_induction_cycles_debit` During DTS, Allowing Free Ingress Induction - (File: `rs/embedders/src/wasmtime_embedder/system_api/sandbox_safe_system_state.rs`)

## Summary

During Deterministic Time Slicing (DTS), when a canister's execution is paused between slices, new ingress messages can arrive and their induction cost is recorded as `ingress_induction_cycles_debit` on the `SystemState`. When the canister subsequently calls `ic0.cycles_burn128`, the burn calculation uses the raw `cycles_balance()` from `SandboxSafeSystemState`, which has no knowledge of the pending debit, allowing the canister to burn cycles already committed to cover postponed ingress induction costs. When `apply_ingress_induction_cycles_debit` is later called, the balance is insufficient and the remaining debit is silently dropped in production builds, making those ingress messages effectively free.

## Finding Description

`SandboxSafeSystemState` is constructed via `SandboxSafeSystemState::new()`, which initializes `initial_cycles_balance` using `system_state.balance()` (the raw balance) rather than `system_state.debited_balance()`:

```rust
// rs/embedders/src/wasmtime_embedder/system_api/sandbox_safe_system_state.rs L836
initial_cycles_balance: system_state.balance(),
```

The struct has no `ingress_induction_cycles_debit` field at all — confirmed by the struct definition at lines 628–661 and the absence of any reference to `ingress_induction_cycles_debit` in the file. The `cycles_balance()` method therefore returns only the raw tracked balance:

```rust
// L893-896
pub(super) fn cycles_balance(&self) -> Cycles {
    let cycles_change = self.system_state_modifications.cycles_balance_change;
    cycles_change.apply(self.initial_cycles_balance)
}
```

`cycles_burn128` uses this raw balance directly:

```rust
// L986-997
let mut new_balance = self.cycles_balance();
let burned_cycles = self.cycles_account_manager.cycles_burn(
    &mut new_balance,
    amount_to_burn,
    ...
);
```

`cycles_burn` then computes `min(amount_to_burn, *cycles_balance - threshold)`, burning up to `balance - freeze_threshold` with no awareness of the pending debit.

After execution, `apply_ingress_induction_cycles_debit` detects the shortfall:

```rust
// system_state.rs L1019-1032
let remaining_debit = self.ingress_induction_cycles_debit - self.cycles_balance;
debug_assert_eq!(remaining_debit.get(), 0);
if remaining_debit.get() > 0 {
    charging_from_balance_error.inc();
    error!(log, "[EXC-BUG]: Debited cycles exceed the cycles balance...");
    // Continue the execution by dropping the remaining debit, which makes
    // some of the postponed charges free.
}
self.consume_cycles(...ingress_induction_cycles_debit...);
self.ingress_induction_cycles_debit = Cycles::zero();
```

The `debug_assert_eq!` is compiled out in production release builds, so the error path is taken silently and the remaining debit is dropped. By contrast, `charge_ingress_induction_cost` correctly uses `debited_balance()` when adding a postponed charge during a paused execution, preventing over-commitment at induction time — but this guard does not protect against the burn path.

## Impact Explanation

The subnet processes ingress messages without collecting their induction fee, breaking the economic invariant that ingress messages must be paid for. The `[EXC-BUG]` error counter is incremented and an error is logged, but execution continues normally with the debit dropped. This constitutes a cycles accounting protocol invariant violation: the subnet absorbs ingress induction costs that should be borne by the canister. The impact per exploit iteration is bounded by the ingress induction fee for messages sent during the DTS pause window, which is small but repeatable. This maps to a **Medium** bounty impact: a meaningful security impact with strict target conditions (DTS execution + `cycles_burn128` call), limited per-iteration economic harm, but a demonstrably broken protocol invariant reachable by an unprivileged user.

## Likelihood Explanation

No privileged access is required. The attacker deploys a canister with a long-running update method (to trigger DTS) that calls `ic0.cycles_burn128` in a later slice — both are standard, unprivileged operations available to any canister developer. DTS is active on all production subnets for long-running executions. Sending ingress messages to a paused canister is a normal, unprivileged operation. The conditions are specific but entirely within the reach of any canister developer. The exploit is repeatable across multiple DTS cycles.

## Recommendation

In `cycles_burn128`, subtract the pending `ingress_induction_cycles_debit` from the available balance before computing how many cycles can be burned. This requires either:

1. Passing `ingress_induction_cycles_debit` into `SandboxSafeSystemState` at construction time (alongside `initial_cycles_balance`) and storing it as a field, then subtracting it in `cycles_burn128` before calling `cycles_account_manager.cycles_burn`.
2. Initializing `initial_cycles_balance` with `system_state.debited_balance()` instead of `system_state.balance()` in `SandboxSafeSystemState::new()`, so the sandbox starts with the already-debited balance.

Option 2 is simpler and mirrors how `charge_ingress_induction_cost` already uses `debited_balance()` to guard against over-commitment.

## Proof of Concept

1. Deploy a canister with a Wasm update method that runs enough instructions to span multiple DTS slices and calls `ic0.cycles_burn128(balance - freeze_threshold)` in a later slice.
2. Send an ingress message to trigger the long-running update. The canister begins DTS execution and is paused after the first slice.
3. While the canister is paused, send one or more additional ingress messages. Each message's induction cost is added via `add_postponed_charge_to_ingress_induction_cycles_debit` on the `SystemState`.
4. The canister resumes. `cycles_burn128` computes available cycles as `cycles_balance - freeze_threshold` (ignoring the pending debit) and burns all of them.
5. After execution, `apply_ingress_induction_cycles_debit` finds `remaining_debit > 0`, logs `[EXC-BUG]`, increments the error counter, and drops the remaining debit.
6. The ingress induction cost of the messages sent in step 3 is never collected.

A deterministic integration test using `ExecutionTestBuilder` with `with_slice_instruction_limit` (as demonstrated in the existing test `dts_ingress_induction_cycles_debit_is_applied_on_replicated_execution_aborts`) can reproduce this by adding a `cycles_burn128` call in the canister Wasm between the pause and the `add_postponed_charge_to_ingress_induction_cycles_debit` step, then asserting the final balance reflects the dropped debit.