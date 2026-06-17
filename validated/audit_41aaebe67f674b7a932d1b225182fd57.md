### Title
CALLCODE with Non-Zero Value in Static Context Silently Succeeds Instead of Reverting — EVM Semantic Mismatch (`evm_interpreter/src/instructions/host.rs`, `zk_ee/src/system/execution_environment/environment_state.rs`)

---

### Summary

The ZKsync OS EVM interpreter does not reject a `CALLCODE` instruction that carries non-zero value when executed inside a static context. Standard EVM (geth, EVMONE) treats this as a write-protection violation and reverts. ZKsync OS instead silently executes the call as a "self-transfer" (caller → caller), allowing the call to succeed. This is a direct EVM semantic mismatch in value-transfer handling, analogous to the reported class of payable/value-forwarding bugs.

---

### Finding Description

**Root cause 1 — missing static-value guard for `CALLCODE`**

In `evm_interpreter/src/instructions/host.rs`, `call_impl` checks for static + non-zero value only for `CallScheme::Call`:

```rust
CallScheme::Call => {
    let value = self.stack.pop_1()?;
    if self.is_static && *value != U256::ZERO {
        return Err(EvmError::CallNotAllowedInsideStatic.into());
    }
    *value
}
CallScheme::CallCode => {
    let value = self.stack.pop_1()?;
    *value          // ← no static check at all
}
```

No equivalent guard exists for `CallScheme::CallCode`. A `CALLCODE` with non-zero value inside a static frame therefore proceeds without error. [1](#0-0) 

**Root cause 2 — `is_transfer_allowed` explicitly permits `EVMCallcodeStatic`**

In `zk_ee/src/system/execution_environment/environment_state.rs`, the transfer-permission predicate explicitly whitelists the `EVMCallcodeStatic` modifier:

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
``` [2](#0-1) 

**Execution path**

When `CALLCODE` is issued inside a static frame with non-zero value, `call_impl` sets `is_static = true` and produces modifier `EVMCallcodeStatic`:

```rust
let is_static = matches!(scheme, CallScheme::StaticCall) || self.is_static;
let call_modifier = if is_static {
    match scheme {
        CallScheme::CallCode => CallModifier::EVMCallcodeStatic,
        ...
    }
}
``` [3](#0-2) 

The request then reaches `perform_transfer_if_required` in the bootloader runner. Because `is_transfer_allowed()` returns `true` for `EVMCallcodeStatic`, the transfer proceeds — from `call_request.caller` to `call_request.caller` (self-transfer):

```rust
let target = match call_request.modifier {
    CallModifier::EVMCallcode | CallModifier::EVMCallcodeStatic => call_request.caller,
    _ => call_request.callee,
};
``` [4](#0-3) 

The call succeeds and returns `1` on the EVM stack, whereas standard EVM would have reverted with `ErrWriteProtection`.

---

### Impact Explanation

**EVM semantic mismatch / valid-execution unprovability divergence.** Any contract that relies on `CALLCODE` with non-zero value reverting inside a static context (e.g., as a reentrancy guard, a capability check, or a try/catch sentinel) will behave differently on ZKsync OS than on Ethereum mainnet. The call succeeds and pushes `1` to the stack instead of reverting, which can cause:

- Incorrect control-flow branching in contracts that catch the revert
- Silent bypass of guards that use `CALLCODE` + value as a write-protection probe
- Forward/proving divergence: the forward runner and the prover both accept the execution, but the resulting state differs from what Ethereum would produce, breaking EVM equivalence guarantees

---

### Likelihood Explanation

`CALLCODE` is a deprecated opcode but remains valid EVM bytecode. Contracts compiled from older Solidity versions or hand-crafted assembly may use it. The specific pattern of `CALLCODE` with non-zero value inside a static context is uncommon but reachable by any unprivileged transaction sender who deploys or calls such a contract on ZKsync OS.

---

### Recommendation

Add the same static-context guard for `CallCode` that already exists for `Call` in `call_impl`:

```rust
CallScheme::CallCode => {
    let value = self.stack.pop_1()?;
    if self.is_static && *value != U256::ZERO {
        return Err(EvmError::CallNotAllowedInsideStatic.into());
    }
    *value
}
```

Remove `CallModifier::EVMCallcodeStatic` from the `is_transfer_allowed` whitelist, or add an assertion that this branch is unreachable once the guard above is in place.

---

### Proof of Concept

Deploy the following bytecode on ZKsync OS and call it via `STATICCALL`:

```
// Contract A (called via STATICCALL from B):
// PUSH1 1        ; value = 1
// PUSH1 0        ; argsOffset
// PUSH1 0        ; argsLen
// PUSH1 0        ; retOffset
// PUSH1 0        ; retLen
// PUSH20 <self>  ; to = self
// PUSH1 0xFFFF   ; gas
// CALLCODE       ; 0xf2 — should revert in static context with value != 0
// PUSH1 0x01
// SSTORE         ; store 1 in slot 0 — should never be reached
```

On Ethereum mainnet: `CALLCODE` with `value=1` in a static context reverts; `SSTORE

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

**File:** evm_interpreter/src/instructions/host.rs (L451-465)
```rust
        let is_static = matches!(scheme, CallScheme::StaticCall) || self.is_static;
        let call_modifier = if is_static {
            match scheme {
                CallScheme::DelegateCall => CallModifier::DelegateStatic,
                CallScheme::CallCode => CallModifier::EVMCallcodeStatic,
                _ => CallModifier::Static,
            }
        } else {
            match scheme {
                CallScheme::Call => CallModifier::NoModifier,
                CallScheme::DelegateCall => CallModifier::Delegate,
                CallScheme::CallCode => CallModifier::EVMCallcode,
                _ => unsafe { unreachable_unchecked() },
            }
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

**File:** basic_bootloader/src/bootloader/runner.rs (L348-351)
```rust
        let target = match call_request.modifier {
            CallModifier::EVMCallcode | CallModifier::EVMCallcodeStatic => call_request.caller,
            _ => call_request.callee,
        };
```
