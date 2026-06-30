### Title
Unhandled ERC-20 `transferFrom` Boolean Return Value in XCC Precompile Allows Silent Transfer Failure - (File: engine-precompiles/src/xcc.rs)

### Summary

The XCC precompile in `engine-precompiles/src/xcc.rs` calls `transferFrom` on the wNEAR ERC-20 contract to move tokens from the user to the engine's implicit address. The EVM call returns a `(exit_reason, return_value)` tuple. The code matches on `exit_reason` and treats `Succeed(_)` as a successful transfer — but never inspects `return_value`, which for an ERC-20 `transferFrom` is the ABI-encoded boolean indicating whether the transfer actually succeeded. If the wNEAR contract returns `false` without reverting, the precompile silently proceeds as if the transfer succeeded.

### Finding Description

In `engine-precompiles/src/xcc.rs` lines 199–203, the XCC precompile executes an EVM-level `transferFrom` call on the wNEAR contract:

```rust
let (exit_reason, return_value) =
    handle.call(wnear_address.raw(), None, tx_data, None, false, &context);
match exit_reason {
    // Transfer successful, nothing to do
    aurora_evm::ExitReason::Succeed(_) => (),
```

The `return_value` is bound but never decoded or validated in the `Succeed` arm. For an ERC-20 `transferFrom`, the ABI return value is `abi.encode(bool)`. If the wNEAR contract returns `false` (transfer failed) without reverting — which is valid per the ERC-20 specification — the precompile treats the call as successful and continues scheduling the NEAR-level promise chain.

The `transfer_from_args` call at lines 188–192 constructs the calldata for `transferFrom(sender, engine_implicit_address, required_near)`. The engine's implicit address would not receive the wNEAR tokens, yet the XCC promise chain proceeds.

### Impact Explanation

If a user's EVM transaction burns ERC-20 tokens (or otherwise modifies EVM state) and then invokes the XCC precompile expecting those tokens to be bridged to NEAR, the following occurs:

1. EVM state changes (e.g., token burn) are committed because the EVM transaction succeeds from the EVM's perspective.
2. The wNEAR `transferFrom` returns `false` silently — the engine's implicit address receives no wNEAR.
3. The NEAR-level wNEAR withdrawal promise subsequently fails (engine has no wNEAR to withdraw).
4. The user's cross-contract call is never executed.
5. The burned/spent EVM tokens are permanently lost with no corresponding NEAR-side action.

This constitutes **permanent freezing/loss of user funds** — a Critical impact.

### Likelihood Explanation

The wNEAR contract on Aurora is an ERC-20 implementation. The ERC-20 standard permits `transferFrom` to return `false` on failure rather than reverting. If the deployed wNEAR contract follows this pattern (e.g., for insufficient balance or missing allowance), the silent failure path is reachable by any unprivileged EVM user who calls the XCC precompile with `required_near > 0` and insufficient wNEAR allowance. The attacker-controlled entry path is: deploy or interact with any EVM contract that calls the XCC precompile (`cross_contract_call::ADDRESS`) with a NEAR payment requirement.

### Recommendation

Decode and assert the boolean return value from the `transferFrom` call in the `Succeed` arm:

```rust
aurora_evm::ExitReason::Succeed(_) => {
    // Decode ABI bool return value and assert it is true
    let success = return_value.last().copied().unwrap_or(0) == 1;
    if !success {
        return Err(PrecompileFailure::Error {
            exit_status: aurora_evm::ExitError::Other(
                Cow::from("ERR_WNEAR_TRANSFER_FAILED")
            ),
        });
    }
}
```

### Proof of Concept

1. User approves the XCC precompile address for 0 wNEAR (or has insufficient balance).
2. User calls an EVM contract that burns ERC-20 tokens and then calls the XCC precompile with `required_near > 0`.
3. The XCC precompile calls `transferFrom` on wNEAR; the wNEAR contract returns `false` (no revert).
4. `exit_reason` is `Succeed`, `return_value` is `0x00...00` (false) — the `Succeed` arm does nothing.
5. The NEAR promise chain is scheduled; the wNEAR withdrawal fails; the cross-contract call is never executed.
6. The user's burned ERC-20 tokens are permanently lost. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** engine-precompiles/src/xcc.rs (L184-216)
```rust
        if required_near != ZERO_YOCTO {
            let engine_implicit_address = aurora_engine_sdk::types::near_account_to_evm_address(
                self.engine_account_id.as_bytes(),
            );
            let tx_data = transfer_from_args(
                sender.0.into(),
                engine_implicit_address.raw().0.into(),
                required_near.as_u128().into(),
            );
            let wnear_address = state::get_wnear_address(&self.io);
            let context = aurora_evm::Context {
                address: wnear_address.raw(),
                caller: cross_contract_call::ADDRESS.raw(),
                apparent_value: U256::zero(),
            };
            let (exit_reason, return_value) =
                handle.call(wnear_address.raw(), None, tx_data, None, false, &context);
            match exit_reason {
                // Transfer successful, nothing to do
                aurora_evm::ExitReason::Succeed(_) => (),
                aurora_evm::ExitReason::Revert(r) => {
                    return Err(PrecompileFailure::Revert {
                        exit_status: r,
                        output: return_value,
                    });
                }
                aurora_evm::ExitReason::Error(e) => {
                    return Err(PrecompileFailure::Error { exit_status: e });
                }
                aurora_evm::ExitReason::Fatal(f) => {
                    return Err(PrecompileFailure::Fatal { exit_status: f });
                }
            }
```

**File:** engine/src/contract_methods/xcc.rs (L33-35)
```rust
        if matches!(handler.promise_result_check(), Some(false)) {
            return Err(b"ERR_CALLBACK_OF_FAILED_PROMISE".into());
        }
```
