### Title
EVM Semantic Mismatch: `CALL_STIPEND` Only Limits Ergs, Not Native Resource, Enabling Full Transaction Revert via Callee Native Exhaustion — (File: `evm_interpreter/src/ee_trait_impl.rs`)

---

### Summary

In ZKsync OS's EVM interpreter, the 2300-gas `CALL_STIPEND` for value-bearing `CALL` opcodes only adds ergs (EVM gas equivalent) to the callee frame. However, native resource (proving cost) is unconditionally and fully transferred from caller to callee. This diverges from standard EVM semantics, where the 2300-gas stipend is the **total** resource available to the callee. An attacker-controlled callee can exhaust the caller's native resource within the 2300-gas ergs budget, causing the **entire transaction** to revert rather than just the sub-call failing gracefully.

---

### Finding Description

**Root cause — ergs-only stipend:**

In `calculate_resources_passed_in_external_call`, when a non-zero-value, non-delegate `CALL` is made, the stipend is computed and stored as ergs only:

```rust
stipend = if !is_delegate && !call_request.nominal_token_value.is_zero() {
    let positive_value_cost = S::Resources::from_ergs(Ergs(CALLVALUE * ERGS_PER_GAS));
    resources_available_in_caller_frame.charge(&positive_value_cost)?;
    Some(Ergs(CALL_STIPEND * ERGS_PER_GAS))   // 2300 * 256 = 588,800 ergs
} else {
    None
};
``` [1](#0-0) 

`resources_to_pass` is then built from ergs only, and the stipend is appended as ergs:

```rust
let mut resources_to_pass = S::Resources::from_ergs(ergs_to_pass);
// ...
if let Some(stipend) = stipend {
    resources_to_pass.add_ergs(stipend);
}
``` [2](#0-1) 

`CALL_STIPEND` is defined as 2300: [3](#0-2) 

**Root cause — unconditional full native transfer:**

Immediately after `calculate_resources_passed_in_external_call` returns, the runner unconditionally moves **all** remaining native resource from the caller frame into the callee frame:

```rust
// Give native resource to the callee.
resources_in_caller_frame.give_native_to(&mut callee_resources);
``` [4](#0-3) 

This is an explicit design property documented in `docs/double_resource_accounting.md`:

> "The native resources are passed fully from frame to frame, a call cannot set a limit on how much of it the callee can spend." [5](#0-4) 

**Consequence:**

The callee receives:
- **Ergs:** `ergs_to_pass + 2300 * ERGS_PER_GAS` (bounded by the stipend)
- **Native:** 100% of the caller's remaining native resource (unbounded)

On standard EVM, the 2300-gas stipend is the *total* resource available to the callee. On ZKsync OS, the callee gets 2300 gas worth of ergs **plus all native resource**. If the callee exhausts native resource (e.g., via cold storage reads), the entire transaction reverts — not just the sub-call.

**Native exhaustion within 2300 gas is realistic:**

From `basic_system/src/system_implementation/flat_storage_model/cost_constants.rs`: [6](#0-5) 

Within 2300 gas (588,800 ergs):
- 1 cold storage read = 2100 gas (537,600 ergs) + **100,000 native**
- 2 warm storage reads = 200 gas (51,200 ergs) + **8,000 native**
- Total native consumed: **~108,000**

For a transaction with `gasLimit = 100,000` and `nativePerGas = 1`, the native limit is exactly 100,000 — less than what the callee can consume within 2300 gas. The callee exhausts native resource, and the entire transaction reverts.

---

### Impact Explanation

On standard EVM, a `transfer()` (or `CALL` with 0 gas + stipend) to a complex recipient fails gracefully: the sub-call reverts, the caller receives `0` on the stack, and execution continues. On ZKsync OS, the same pattern can cause the **entire transaction** to revert because native resource exhaustion in any frame is a fatal, transaction-wide error. This breaks the security assumption underlying `transfer()` patterns:

- Contracts that use `transfer()` to send ETH to user-provided addresses can be permanently griefed.
- Withdrawal flows, payment splitters, and auction settlement contracts that rely on graceful `transfer()` failure handling are vulnerable.
- An attacker can prevent a victim contract from completing any transaction that includes a value transfer to an attacker-controlled address.

---

### Likelihood Explanation

**Medium.** The attacker must:
1. Control the recipient contract (deploy a contract with a native-heavy fallback).
2. Induce a victim contract to call `transfer()` (or `CALL` with 0 gas + value) to the attacker's address.

Condition 1 is trivially achievable. Condition 2 is common in DeFi patterns (withdrawal queues, fee distribution, auction settlement). The native resource constraint is met for any transaction whose gas limit is ≤ ~100,000 gas at `nativePerGas ≥ 1`, which covers a large fraction of real transactions.

---

### Recommendation

Limit the native resource passed to the callee proportionally to the ergs passed, rather than passing the full native resource unconditionally. For example:

```
native_to_pass = total_native * min(ergs_to_pass, max_passable_ergs) / total_ergs
```

This would ensure that a callee receiving only the 2300-gas stipend cannot consume more native resource than is proportional to its ergs budget, matching standard EVM semantics where the stipend is the total resource cap.

---

### Proof of Concept

```solidity
// Attacker contract
contract NativeExhauster {
    uint256 private slot0;
    fallback() external payable {
        // Cold storage read: 2100 gas, ~100,000 native
        uint256 val = slot0;
        // Warm reads to fill remaining 200 gas
        val = slot0; val = slot0;
    }
}

// Victim contract (uses transfer-equivalent pattern)
contract Victim {
    function withdraw(address payable recipient) external {
        // CALL with 0 gas + stipend (transfer equivalent)
        (bool ok,) = recipient.call{value: 1 wei, gas: 0}("");
        require(ok, "transfer failed"); // never reached
    }
}
```

**Attack steps:**
1. Deploy `NativeExhauster` at address `A`.
2. Fund `Victim` with ETH.
3. Call `Victim.withdraw(A)` with `gasLimit ≤ 100,000`.
4. `Victim` sends 1 wei to `A` with 0 gas (stipend = 2300 gas ergs + full native).
5. `A`'s fallback performs 1 cold + 2 warm storage reads, consuming ~108,000 native.
6. Native resource exhausted → `FatalRuntimeError` → entire transaction reverts.
7. `Victim.withdraw` never completes; funds remain locked.

### Citations

**File:** evm_interpreter/src/ee_trait_impl.rs (L301-308)
```rust
            // Positive value cost and stipend
            stipend = if !is_delegate && !call_request.nominal_token_value.is_zero() {
                let positive_value_cost = S::Resources::from_ergs(Ergs(CALLVALUE * ERGS_PER_GAS));
                resources_available_in_caller_frame.charge(&positive_value_cost)?;
                Some(Ergs(CALL_STIPEND * ERGS_PER_GAS))
            } else {
                None
            };
```

**File:** evm_interpreter/src/ee_trait_impl.rs (L330-340)
```rust
        let mut resources_to_pass = S::Resources::from_ergs(ergs_to_pass);

        // This never panics because max_passable_ergs <= resources_available_in_caller_frame
        resources_available_in_caller_frame
            .charge(&resources_to_pass)
            .unwrap();

        // Add stipend
        if let Some(stipend) = stipend {
            resources_to_pass.add_ergs(stipend);
        }
```

**File:** evm_interpreter/src/gas_constants.rs (L45-45)
```rust
pub const CALL_STIPEND: u64 = 2300;
```

**File:** basic_bootloader/src/bootloader/runner.rs (L703-705)
```rust
        // Give native resource to the callee.
        resources_in_caller_frame.give_native_to(&mut callee_resources);
        callee_resources
```

**File:** docs/double_resource_accounting.md (L21-21)
```markdown
The native resources are passed fully from frame to frame, a call cannot set a limit on how much of it the callee can spend.
```

**File:** basic_system/src/system_implementation/flat_storage_model/cost_constants.rs (L17-22)
```rust
pub const COLD_EXISTING_STORAGE_READ_NATIVE_COST: u64 = native_with_delegations!(100_000, 0, 1320);
pub const COLD_NEW_STORAGE_READ_NATIVE_COST: u64 = 2 * COLD_EXISTING_STORAGE_READ_NATIVE_COST;
pub const COLD_EXISTING_STORAGE_WRITE_EXTRA_NATIVE_COST: u64 =
    native_with_delegations!(40_000, 0, 660);
pub const COLD_NEW_STORAGE_WRITE_EXTRA_NATIVE_COST: u64 =
    native_with_delegations!(100_000, 0, 1300);
```
