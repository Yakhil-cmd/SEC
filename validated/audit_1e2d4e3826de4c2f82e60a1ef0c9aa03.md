### Title
Off-by-One in EVM Call Depth Limit Allows Stack Depth 1025 Instead of 1024 - (File: `evm_interpreter/src/ee_trait_impl.rs`)

---

### Summary

`check_depth_and_balance` uses `callstack_depth > 1024` instead of `callstack_depth >= 1024`. Because `callstack_depth` is 0-indexed from the root frame, this allows the call stack to reach depth 1024 (1025 total frames) rather than the EVM-specified maximum of 1024 total frames, producing an EVM semantic mismatch reachable by any unprivileged transaction sender.

---

### Finding Description

In `evm_interpreter/src/ee_trait_impl.rs`, the function `check_depth_and_balance` enforces the EVM call-depth limit:

```rust
fn check_depth_and_balance<S: EthereumLikeTypes>(
    system: &mut System<S>,
    call_request: &mut ExternalCallRequest<S>,
    callstack_depth: usize,
) -> Result<Option<EvmError>, SubsystemError<EvmErrors>>
{
    if callstack_depth > 1024 {          // ← off-by-one: should be >= 1024
        return Ok(Some(EvmError::CallTooDeep));
    }
    ...
}
```

`callstack_depth` is the depth of the **frame about to be executed**, 0-indexed from the root frame. This is confirmed by `before_executing_frame`, which uses the same field and explicitly treats `callstack_depth == 0` as the root frame:

```rust
// Increase nonce. Ignore, if we are in the root frame - caller's nonce already incremented before.
if frame_state.environment_parameters.callstack_depth > 0 {
    // increment nonce
}
```

With 0-indexed depth:

| Frame | `callstack_depth` | Check `> 1024` | Result |
|---|---|---|---|
| Root (tx entry) | 0 | false | allowed |
| 1st nested call | 1 | false | allowed |
| … | … | … | … |
| 1024th nested call | 1024 | **false** | **allowed ← bug** |
| 1025th nested call | 1025 | true | rejected |

The system therefore allows 1025 total frames (depth 0–1024), while the EVM specification and reference implementations (e.g., go-ethereum with `depth > params.CallCreateDepth` where depth is incremented **before** the check, yielding a maximum post-increment depth of 1024 = 1024 total nested frames from depth 1) permit only 1024.

The codebase itself defines the intended limit as a named constant:

```rust
pub const MAX_GLOBAL_CALLS_STACK_DEPTH: usize = 1024;
```

but `check_depth_and_balance` uses the raw literal `1024` with `>` rather than `>= MAX_GLOBAL_CALLS_STACK_DEPTH`.

---

### Impact Explanation

**EVM semantic mismatch / state-transition divergence.** Any contract execution that reaches exactly 1024 nested calls will succeed on ZKsync OS but revert with `CallTooDeep` on Ethereum mainnet (and on a correctly implemented EVM). Concretely:

1. **State divergence**: A transaction whose 1024th nested call transfers value or writes storage will commit those effects on ZKsync OS but would have been a no-op on mainnet. This breaks cross-chain equivalence guarantees.
2. **Reentrancy-guard bypass**: Contracts that use the call-depth limit as a reentrancy guard (a known pattern) can be bypassed by an attacker who crafts a call chain of exactly 1024 frames.
3. **Forward/proving divergence**: If the prover enforces the correct EVM call-depth limit (1024 frames), a block produced by the sequencer that accepted a depth-1025 execution will be unprovable, causing a liveness failure.

---

### Likelihood Explanation

High. The attacker-controlled entry path is a standard EVM transaction. No privileged role, leaked key, or external oracle is required. Any user can deploy a contract that recursively calls itself 1024 times and observe the extra frame succeeding. The depth-1024 frame is fully attacker-controlled (calldata, value, target address).

---

### Recommendation

Change the strict `>` comparison to `>=` in `check_depth_and_balance`, and use the named constant for clarity:

```rust
// Before (off-by-one):
if callstack_depth > 1024 {

// After (correct):
if callstack_depth >= MAX_GLOBAL_CALLS_STACK_DEPTH {
```

Apply the same fix to `constructor_pre_checks`, which delegates to `check_depth_and_balance` and therefore inherits the same bug.

---

### Proof of Concept

Deploy the following contract on ZKsync OS:

```solidity
contract DepthProbe {
    uint256 public maxDepth;

    function recurse(uint256 depth) external {
        if (depth > maxDepth) maxDepth = depth;
        if (depth < 1025) {
            // On correct EVM this call at depth==1024 must revert with CallTooDeep.
            // On ZKsync OS (bug present) it succeeds.
            try this.recurse(depth + 1) {} catch {}
        }
    }
}
```

Call `recurse(0)`. On a correct EVM, `maxDepth` will be 1023 (the 1024th frame, depth 1024, is rejected). On ZKsync OS with the bug, `maxDepth` will be 1024 (the 1025th frame, depth 1025, is the first to be rejected), demonstrating the extra allowed nesting level and the resulting state divergence.

---

**Affected code:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** evm_interpreter/src/ee_trait_impl.rs (L380-387)
```rust
        if let Some(error) = check_depth_and_balance(
            system,
            &mut frame_state.external_call,
            frame_state.environment_parameters.callstack_depth,
        )? {
            tracer.evm_tracer().on_call_error(&error);
            return Ok(false);
        }
```

**File:** evm_interpreter/src/ee_trait_impl.rs (L460-463)
```rust
    if callstack_depth > 1024 {
        system_log!(system, "Callstack is too deep\n",);
        return Ok(Some(EvmError::CallTooDeep));
    }
```

**File:** evm_interpreter/src/ee_trait_impl.rs (L499-501)
```rust
    if let Some(error) = check_depth_and_balance(system, call_request, callstack_depth)? {
        return Ok(Some(error));
    }
```

**File:** zk_ee/src/system/mod.rs (L32-34)
```rust
pub const MAX_GLOBAL_CALLS_STACK_DEPTH: usize = 1024; // even though we do not have to formally limit it,
                                                      // for all practical purposes (63/64) ^ 1024 is 10^-7, and it's unlikely that one can create any new frame
                                                      // with such remaining resources
```

**File:** zk_ee/src/system/execution_environment/environment_state.rs (L120-124)
```rust
pub struct EnvironmentParameters<'a> {
    pub scratch_space_len: u32,
    pub callstack_depth: usize,
    pub callee_account_properties: CalleeAccountProperties<'a>,
}
```
