### Title
`CALLCODE` with Non-Zero Value Bypasses the Static-Context Guard — (`evm_interpreter/src/instructions/host.rs`)

---

### Summary

The `CALL` opcode correctly rejects non-zero value transfers inside a static context. The `CALLCODE` opcode does not perform this check. `is_transfer_allowed()` explicitly permits `EVMCallcodeStatic` with a non-zero value, creating an EVM semantic mismatch: the EVM specification and the go-ethereum reference implementation both require `CALLCODE` with non-zero value to revert in a static context.

---

### Finding Description

In `call_impl` (`evm_interpreter/src/instructions/host.rs`), the value is extracted from the stack differently per scheme:

```rust
let value = match scheme {
    CallScheme::CallCode => {
        let value = self.stack.pop_1()?;
        *value                          // ← no static guard
    }
    CallScheme::Call => {
        let value = self.stack.pop_1()?;
        if self.is_static && *value != U256::ZERO {
            return Err(EvmError::CallNotAllowedInsideStatic.into()); // ← guarded
        }
        *value
    }
    ...
};
```

`CALL` with non-zero value is rejected when `self.is_static` is set. `CALLCODE` with non-zero value is not checked at all.

When `self.is_static` is true and the scheme is `CallCode`, the modifier is set to `EVMCallcodeStatic`. The downstream `is_transfer_allowed()` function then explicitly permits this modifier:

```rust
pub fn is_transfer_allowed(&self) -> bool {
    ...
    // Positive-value callcode calls are allowed in static context,
    // as the transfer is a self-transfer.
    || self.modifier == CallModifier::EVMCallcodeStatic
}
```

The transfer proceeds: `perform_transfer_if_required` calls `transfer_nominal_token_value` from `caller` to `caller` (because CALLCODE redirects the target to the caller). The callee frame is then launched with `is_static = true`.

This is the direct analog of the external report: `CALL` (the "deposit" path) checks the guard; `CALLCODE` (the "internal withdraw" path that also moves value) does not.

---

### Impact Explanation

The EVM specification (EIP-214) states that any state-modifying operation in a static context must throw an exception. Value transfer is a state-modifying operation. The go-ethereum reference implementation applies the write-protection check to `CALLCODE` identically to `CALL`:

```go
if interpreter.readOnly && value.Sign() != 0 {
    return nil, ErrWriteProtection
}
```

ZKsync OS does not revert. Contracts that rely on `CALLCODE` with non-zero value reverting inside a `STATICCALL` context will behave differently on ZKsync OS than on Ethereum. This is a forward/proving divergence and EVM semantic mismatch.

Practical severity is bounded: the transfer is a self-transfer (caller → caller), so no net balance change occurs, and the callee still executes under `is_static = true`. However, the non-revert itself is observable by the calling contract (the CALLCODE returns success instead of failure), which can alter control flow in contracts that use this as a guard. The deviation is not listed in `docs/not-a-bug.md`.

---

### Likelihood Explanation

Any unprivileged user can deploy a contract that issues `CALLCODE` with non-zero value and then invoke it via `STATICCALL`. No privileged role, oracle manipulation, or external dependency is required. The divergence is deterministic and reproducible.

---

### Recommendation

Add the same static-context guard to `CallScheme::CallCode` that already exists for `CallScheme::Call`:

```rust
CallScheme::CallCode => {
    let value = self.stack.pop_1()?;
    if self.is_static && *value != U256::ZERO {
        return Err(EvmError::CallNotAllowedInsideStatic.into());
    }
    *value
}
```

Remove `CallModifier::EVMCallcodeStatic` from the `is_transfer_allowed` allowlist, or add a comment explaining the intentional deviation and document it in `docs/not-a-bug.md` if the self-transfer behavior is deliberately kept.

---

### Proof of Concept

```solidity
// Victim contract — expects CALLCODE with value to revert in static context
contract Victim {
    uint256 public flag;

    function probe() external {
        // CALLCODE with value=1 to self
        assembly {
            let ok := callcode(gas(), address(), 1, 0, 0, 0, 0)
            // On Ethereum: ok == 0 (reverted by static guard)
            // On ZKsync OS: ok == 1 (proceeds, self-transfer)
            if ok { sstore(0, 1) }   // sets flag=1 — unreachable on Ethereum
        }
    }
}
```

1. Deploy `Victim`.
2. Call `probe()` via `STATICCALL` from another contract.
3. On Ethereum: the inner `callcode` reverts → `ok == 0` → `flag` stays `0`.
4. On ZKsync OS: the inner `callcode` succeeds → `ok == 1` → the `sstore` is attempted (which will itself revert due to `is_static`, but the control-flow branch is taken differently, and the return value of the `callcode` differs from Ethereum).

The observable divergence is the return value of the `CALLCODE` opcode inside a static context with non-zero value.

---

**Root cause files:** [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** evm_interpreter/src/instructions/host.rs (L416-430)
```rust
        let value = match scheme {
            CallScheme::CallCode => {
                let value = self.stack.pop_1()?;
                *value
            }
            CallScheme::Call => {
                let value = self.stack.pop_1()?;
                if self.is_static && *value != U256::ZERO {
                    return Err(EvmError::CallNotAllowedInsideStatic.into());
                }
                *value
            }
            CallScheme::DelegateCall => self.call_value,
            CallScheme::StaticCall => U256::ZERO,
        };
```

**File:** zk_ee/src/system/execution_environment/environment_state.rs (L61-69)
```rust
    pub fn is_transfer_allowed(&self) -> bool {
        self.modifier == CallModifier::NoModifier
        || self.modifier == CallModifier::Constructor
        || self.modifier == CallModifier::ZKVMSystem
        || self.modifier == CallModifier::EVMCallcode
        // Positive-value callcode calls are allowed in static context,
        // as the transfer is a self-transfer.
        || self.modifier == CallModifier::EVMCallcodeStatic
    }
```

**File:** basic_bootloader/src/bootloader/runner.rs (L333-351)
```rust
        if call_request.nominal_token_value.is_zero() || call_request.is_delegate() {
            return Ok(true);
        }

        // Check transfer is allowed and determine transfer target
        if !call_request.is_transfer_allowed() {
            system_log!(
                self.system,
                "Call failed: positive value with modifier {:?}\n",
                call_request.modifier
            );
            return Err(internal_error!("Positive value with incorrect modifier").into());
        }
        // Adjust transfer target due to CALLCODE
        // TODO: in future should be moved to EE
        let target = match call_request.modifier {
            CallModifier::EVMCallcode | CallModifier::EVMCallcodeStatic => call_request.caller,
            _ => call_request.callee,
        };
```
