### Title
CALL_STIPEND Bounds Only EVM Gas, Not Native Resource — Malicious Fallback Can Exhaust Transaction-Level Native Budget via Value-Transfer Call - (File: evm_interpreter/src/ee_trait_impl.rs)

---

### Summary

ZKsync OS implements a dual resource model: EVM gas (ergs) and a native resource (proving cost). When a non-zero value CALL is made, the callee receives the standard EVM 2300-gas stipend added to its ergs. However, the native resource is **passed fully from frame to frame with no per-call cap**. Within the 2300-gas stipend, a malicious recipient's fallback function can perform a cold SLOAD (2100 gas, ~100,000 native units), exhausting the transaction's native budget and causing the **entire transaction to revert** — not merely the sub-call.

---

### Finding Description

In `calculate_resources_passed_in_external_call`, when a non-zero value CALL is made, the stipend is constructed and added exclusively to the ergs dimension of `resources_to_pass`:

```rust
// ee_trait_impl.rs lines 302–340
stipend = if !is_delegate && !call_request.nominal_token_value.is_zero() {
    let positive_value_cost = S::Resources::from_ergs(Ergs(CALLVALUE * ERGS_PER_GAS));
    resources_available_in_caller_frame.charge(&positive_value_cost)?;
    Some(Ergs(CALL_STIPEND * ERGS_PER_GAS))   // 2300 gas in ergs only
} else {
    None
};
// ...
let mut resources_to_pass = S::Resources::from_ergs(ergs_to_pass); // ergs only, no native
// ...
if let Some(stipend) = stipend {
    resources_to_pass.add_ergs(stipend);       // still ergs only
}
``` [1](#0-0) 

`resources_to_pass` is built with `S::Resources::from_ergs(...)`, which carries **zero native resource**. The native resource is passed separately and fully, as documented:

> "The native resources are passed fully from frame to frame, a call cannot set a limit on how much of it the callee can spend." [2](#0-1) 

And exhaustion of native resource is transaction-fatal:

> "If a transaction execution runs out of native resources, the entire transaction is reverted." [3](#0-2) 

The `CALL_STIPEND` constant is 2300 gas: [4](#0-3) 

The `sstore` opcode has an explicit guard against execution within the stipend:

```rust
if self.gas.gas_left() <= CALL_STIPEND {
    return Err(EvmError::InvalidOperandOOG.into());
}
``` [5](#0-4) 

**No equivalent guard exists for SLOAD.** A cold SLOAD costs 2100 EVM gas (`COLD_SLOAD_COST = 2100`), which is within the 2300-gas stipend, but its native cost is `COLD_EXISTING_STORAGE_READ_NATIVE_COST = native_with_delegations!(100_000, 0, 1320)` — orders of magnitude larger than the ergs cost. [6](#0-5) [7](#0-6) 

---

### Impact Explanation

Any on-chain contract that uses a Solidity `.transfer()` or `.send()` pattern (CALL with 2300-gas stipend) to send ETH to an attacker-controlled address is vulnerable. The attacker deploys a contract whose `receive()`/`fallback()` performs a single cold SLOAD (2100 gas ≤ 2300 stipend). This consumes ~100,000 native units from the **shared transaction-level native budget**, triggering a full transaction revert. Because the native resource cannot be isolated per sub-call, the caller cannot catch or recover from this failure. Any protocol function that distributes ETH to user-supplied addresses (e.g., withdrawal, fee distribution, auction settlement) can be permanently bricked for any transaction that includes such a recipient.

---

### Likelihood Explanation

The attacker only needs to be the recipient of an ETH transfer from a contract using the `.transfer()`/`.send()` pattern. This is a common Solidity idiom. The attacker deploys a one-instruction fallback (`SLOAD` of a cold slot). No privileged access, no governance, no oracle manipulation is required. The native budget is derived from `gasLimit × nativePerGas`; for transactions with a modest gas limit or low gas price, a single cold SLOAD native charge is sufficient to exhaust it. Likelihood is **medium-high** given the prevalence of the pattern and the low attacker cost.

---

### Recommendation

1. **Bound native resource passed to callees in value-transfer calls**, analogous to how ergs are bounded by the stipend. When `resources_to_pass` is constructed for a stipend-only call, the native component should be capped (e.g., proportional to the stipend gas amount times a worst-case native-per-gas ratio).

2. Alternatively, **add a native-resource guard for SLOAD** (similar to the existing SSTORE guard) when the remaining EVM gas is at or below `CALL_STIPEND`, preventing cold storage reads within the stipend frame.

3. Document clearly that the 2300-gas stipend does **not** protect against native resource exhaustion, so contract authors on ZKsync OS are aware that `.transfer()`/`.send()` to untrusted addresses carries a different risk profile than on mainnet Ethereum.

---

### Proof of Concept

```solidity
// Attacker contract
contract MaliciousFallback {
    uint256 slot0; // cold slot
    receive() external payable {
        // Cold SLOAD: 2100 EVM gas (within 2300 stipend)
        // but ~100,000 native units consumed from tx-level budget
        assembly { pop(sload(0)) }
    }
}

// Victim contract (common pattern)
contract Victim {
    function withdraw(address payable recipient) external {
        recipient.transfer(1 wei); // 2300-gas stipend CALL
        // If recipient is MaliciousFallback:
        //   EVM gas check: 2100 < 2300 → passes
        //   Native resource: ~100,000 consumed → tx reverts entirely
    }
}
```

**Attack steps:**
1. Attacker deploys `MaliciousFallback` at address `A`.
2. Attacker calls `Victim.withdraw(A)` with a transaction whose native budget is tight (low gas price or modest gas limit).
3. The `transfer` issues a CALL with 2300-gas stipend to `A`.
4. `MaliciousFallback.receive()` executes `SLOAD(0)` — 2100 EVM gas consumed (within stipend), but ~100,000 native units consumed from the shared transaction budget.
5. Native budget exhausted → entire transaction reverts at the system level.
6. `Victim.withdraw` is permanently DoS'd for any call that routes through `A`.

### Citations

**File:** evm_interpreter/src/ee_trait_impl.rs (L302-340)
```rust
            stipend = if !is_delegate && !call_request.nominal_token_value.is_zero() {
                let positive_value_cost = S::Resources::from_ergs(Ergs(CALLVALUE * ERGS_PER_GAS));
                resources_available_in_caller_frame.charge(&positive_value_cost)?;
                Some(Ergs(CALL_STIPEND * ERGS_PER_GAS))
            } else {
                None
            };

            // Account creation cost
            let callee_is_empty = callee_parameters.nonce == 0
                && callee_parameters.unpadded_code_len == 0
                && callee_parameters.nominal_token_balance.is_zero();
            if !is_callcode_or_delegate
                && !call_request.nominal_token_value.is_zero()
                && callee_is_empty
            {
                let callee_creation_cost = S::Resources::from_ergs(Ergs(NEWACCOUNT * ERGS_PER_GAS));
                resources_available_in_caller_frame.charge(&callee_creation_cost)?
            }
        }

        // we just need to apply 63/64 rule, as System/IO is responsible for the rest

        let max_passable_ergs =
            gas_utils::apply_63_64_rule(resources_available_in_caller_frame.ergs());
        let ergs_to_pass = core::cmp::min(call_request.ergs_to_pass, max_passable_ergs);

        // Charge caller frame
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

**File:** docs/double_resource_accounting.md (L19-19)
```markdown
If a transaction execution runs out of native resources, the entire transaction is reverted. If the same happens during transaction validation, the transaction is considered invalid.
```

**File:** docs/double_resource_accounting.md (L21-21)
```markdown
The native resources are passed fully from frame to frame, a call cannot set a limit on how much of it the callee can spend.
```

**File:** evm_interpreter/src/gas_constants.rs (L38-38)
```rust
pub const COLD_SLOAD_COST: u64 = 2100;
```

**File:** evm_interpreter/src/gas_constants.rs (L45-45)
```rust
pub const CALL_STIPEND: u64 = 2300;
```

**File:** evm_interpreter/src/instructions/host.rs (L157-159)
```rust
        if self.gas.gas_left() <= CALL_STIPEND {
            return Err(EvmError::InvalidOperandOOG.into());
        }
```

**File:** basic_system/src/system_implementation/flat_storage_model/cost_constants.rs (L17-17)
```rust
pub const COLD_EXISTING_STORAGE_READ_NATIVE_COST: u64 = native_with_delegations!(100_000, 0, 1320);
```
