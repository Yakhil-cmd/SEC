### Title
`CrossContractCall` Precompile Ignores `transferFrom` Boolean Return Value - (File: `engine-precompiles/src/xcc.rs`)

### Summary

The `CrossContractCall` precompile in `engine-precompiles/src/xcc.rs` calls `transferFrom` on the wNEAR ERC-20 contract to collect payment from the caller before scheduling a NEAR cross-contract call. After the EVM subcall, the code only checks the EVM-level exit reason (`Succeed`/`Revert`/`Error`/`Fatal`) but never decodes or validates the ABI-encoded boolean return value. If the wNEAR contract returns `false` from `transferFrom` without reverting — a valid behavior for non-standard or future ERC-20 implementations — the precompile silently proceeds, scheduling the cross-contract call without collecting the required NEAR payment.

### Finding Description

In `CrossContractCall::run_with_handle`, when `required_near != ZERO_YOCTO`, the precompile issues an EVM subcall to the wNEAR ERC-20 contract's `transferFrom` selector:

```rust
let (exit_reason, return_value) =
    handle.call(wnear_address.raw(), None, tx_data, None, false, &context);
match exit_reason {
    // Transfer successful, nothing to do
    aurora_evm::ExitReason::Succeed(_) => (),   // ← return_value is NEVER checked
    aurora_evm::ExitReason::Revert(r) => { ... }
    aurora_evm::ExitReason::Error(e) => { ... }
    aurora_evm::ExitReason::Fatal(f) => { ... }
}
``` [1](#0-0) 

The `return_value` variable holds the raw ABI-encoded bytes returned by the EVM call — for a standard ERC-20 `transferFrom`, this is a 32-byte ABI-encoded `bool`. When `ExitReason::Succeed(_)` is matched, the arm is `()` — the return value is discarded entirely. A wNEAR implementation that returns `false` (32 bytes of zeros) without reverting would cause the precompile to proceed as if the transfer succeeded.

The `required_near` amount includes both the NEAR attached to the cross-contract call (`attached_near`) and, for first-time callers, the 2 NEAR storage staking deposit (`STORAGE_AMOUNT`):

```rust
let required_near =
    match state::get_code_version_of_address(&self.io, &Address::new(sender)) {
        None => attached_near + state::STORAGE_AMOUNT,
        Some(_) => attached_near,
    };
``` [2](#0-1) 

After the unchecked `transferFrom`, the precompile unconditionally emits the promise log that schedules the NEAR cross-contract call:

```rust
Ok(PrecompileOutput {
    logs: vec![promise_log],
    cost,
    ..Default::default()
})
``` [3](#0-2) 

### Impact Explanation

**High — Theft of unclaimed yield / Temporary freezing of funds.**

If `transferFrom` returns `false` without reverting, the caller's wNEAR balance is not debited, but the promise log is still emitted. The NEAR runtime processes this log and attempts to execute the cross-contract call. The engine's implicit address is expected to hold the collected wNEAR to back the XCC router's storage and execution costs. A caller who bypasses the payment:

1. Gets cross-contract calls to NEAR contracts funded by the engine's existing NEAR balance rather than their own wNEAR.
2. For first-time callers, avoids the 2 NEAR storage staking deposit (`STORAGE_AMOUNT`), draining the engine's NEAR reserves.

This constitutes direct theft of NEAR held by the engine's implicit account.

### Likelihood Explanation

**Medium.** The current production wNEAR on Aurora is based on OpenZeppelin's ERC-20 (via `EvmErc20`), which reverts on failure rather than returning `false`. However:

- The wNEAR address is configurable via `factory_set_wnear_address` and can be pointed at any ERC-20 contract.
- Any future wNEAR implementation or bridged token variant that follows the older ERC-20 pattern (returning `false` on failure instead of reverting) would trigger this silently.
- A contract that returns empty bytes (zero-length `return_value`) on a successful EVM call — e.g., a proxy or wrapper — would also pass the check with no transfer occurring.

The attack path is fully unprivileged: any EVM user who calls the XCC precompile address triggers this code path.

### Recommendation

After the `ExitReason::Succeed` arm, decode `return_value` as an ABI-encoded `bool` and revert if it is `false` or if the return data is not exactly 32 bytes:

```rust
aurora_evm::ExitReason::Succeed(_) => {
    // Decode the bool return value of transferFrom
    let transfer_ok = return_value.len() == 32
        && return_value[31] == 1
        && return_value[..31].iter().all(|b| *b == 0);
    if !transfer_ok {
        return Err(revert_with_message("ERR_WNEAR_TRANSFER_FAILED"));
    }
}
```

This mirrors the `safeTransferFrom` pattern recommended in the original report: always validate the boolean return value of ERC-20 calls regardless of whether the EVM call itself succeeded.

### Proof of Concept

1. Deploy a non-standard wNEAR ERC-20 contract on Aurora whose `transferFrom` always returns `false` (without reverting).
2. Call `factory_set_wnear_address` to register it (or assume a future upgrade does so).
3. As an unprivileged EVM user, call the XCC precompile at `0x516cded1d16af10cad47d6d49128e2eb7d27b372` with a valid `CrossContractCallArgs::Eager` payload specifying `attached_near > 0`.
4. The precompile calls `transferFrom` on the malicious wNEAR; the EVM call returns `ExitReason::Succeed` with `return_value = [0u8; 32]`.
5. The `match` arm hits `aurora_evm::ExitReason::Succeed(_) => ()` — no check on `return_value`.
6. The promise log is emitted; the NEAR runtime schedules the cross-contract call.
7. The caller's wNEAR was never debited; the engine's NEAR balance funds the call. [4](#0-3)

### Citations

**File:** engine-precompiles/src/xcc.rs (L177-182)
```rust
        let required_near =
            match state::get_code_version_of_address(&self.io, &Address::new(sender)) {
                // If there is no deployed version of the router contract then we need to charge for storage staking
                None => attached_near + state::STORAGE_AMOUNT,
                Some(_) => attached_near,
            };
```

**File:** engine-precompiles/src/xcc.rs (L183-217)
```rust
        // if some NEAR payment is needed, transfer it from the caller to the engine's implicit address
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
        }
```

**File:** engine-precompiles/src/xcc.rs (L233-237)
```rust
        Ok(PrecompileOutput {
            logs: vec![promise_log],
            cost,
            ..Default::default()
        })
```
