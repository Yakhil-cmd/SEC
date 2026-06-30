### Title
ETH Permanently Frozen When `refund_on_error` Sends ETH to a Contract Without `receive()` - (File: `engine/src/engine.rs`)

### Summary

When an `ExitToNear` precompile call carrying ETH fails on the NEAR side, `refund_on_error` attempts to return the ETH to the original caller via an EVM `call` with empty calldata. If the caller is an EVM contract that lacks a `receive()` or payable `fallback()` function, the refund call reverts and the ETH is permanently frozen inside the `exit_to_near` precompile address.

### Finding Description

`refund_on_error` in `engine/src/engine.rs` handles the ETH-exit failure path as follows:

```rust
} else {
    // ETH exit; transfer ETH back from precompile address
    let exit_address = exit_to_near::ADDRESS;
    ...
    engine.call(
        &exit_address,
        &refund_address,   // ← original caller / contract
        amount,
        Vec::new(),        // ← empty calldata triggers receive()
        u64::MAX,
        ...
    )
}
``` [1](#0-0) 

An EVM `call` with a non-zero value and empty calldata dispatches to the target's `receive()` function. If `refund_address` is a contract that has no `receive()` or payable `fallback()`, the EVM reverts the call. The caller in `exit_to_near_precompile_callback` then returns `ERR_REFUND_FAILURE`:

```rust
} else if let Some(args) = args.refund {
    let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;
    if !refund_result.status.is_ok() {
        return Err(errors::ERR_REFUND_FAILURE.into());
    }
``` [2](#0-1) 

At the point the callback fires, the ETH has already been debited from the user's EVM balance and credited to `exit_to_near::ADDRESS` during the original precompile execution. A failed refund leaves it stranded there with no recovery path.

The `error_refund` feature gate controls whether a `RefundCallArgs` is even populated:

```rust
#[cfg(feature = "error_refund")]
refund: refund_call_args(&exit_to_near_params, &exit_event),
#[cfg(not(feature = "error_refund"))]
refund: None,
``` [3](#0-2) 

When `error_refund` is enabled (the production-intended path for safe exits), the refund is attempted but can silently fail for any contract caller lacking `receive()`.

### Impact Explanation

**Critical — Permanent freezing of funds.**

ETH sent through the `ExitToNear` precompile from a contract address without `receive()` is irrecoverably locked in `exit_to_near::ADDRESS` whenever the NEAR-side transfer fails. There is no admin escape hatch or secondary recovery mechanism for funds stranded at a precompile address.

### Likelihood Explanation

**Medium.** Many EVM contracts (multisigs, DAOs, custom vaults) deliberately omit `receive()` to prevent accidental ETH acceptance. Any such contract that bridges ETH to NEAR and targets an unregistered or invalid NEAR account will trigger this path. The NEAR-side failure condition (unregistered recipient, paused token, etc.) is easy to reach accidentally or by a griefing third party who controls the NEAR recipient account.

### Recommendation

Replace the bare `engine.call` with empty data with a direct balance credit, bypassing the EVM `receive()` dispatch entirely:

```rust
// Instead of engine.call(..., Vec::new(), ...)
add_balance(&mut engine.io, &refund_address, amount)?;
```

This mirrors how `refund_unused_gas` credits balances without triggering contract code, and is the correct primitive for an internal bookkeeping refund. [4](#0-3) 

### Proof of Concept

1. Deploy an EVM contract `Vault` on Aurora that has **no** `receive()` function and calls `ExitToNear` precompile with 1 ETH targeting `"unregistered.near"`.
2. `Vault` calls the precompile; ETH is debited from `Vault`'s EVM balance and held at `exit_to_near::ADDRESS`.
3. The NEAR `ft_transfer` to `"unregistered.near"` fails (account not registered).
4. `exit_to_near_precompile_callback` fires; `refund_on_error` calls `engine.call(exit_address, vault_address, 1 ETH, [], ...)`.
5. The EVM reverts because `Vault` has no `receive()`.
6. `exit_to_near_precompile_callback` returns `ERR_REFUND_FAILURE`.
7. `Vault`'s EVM balance is 0; `exit_to_near::ADDRESS` holds 1 ETH permanently.

### Citations

**File:** engine/src/engine.rs (L1204-1224)
```rust
    } else {
        // ETH exit; transfer ETH back from precompile address
        let exit_address = exit_to_near::ADDRESS;
        let mut engine: Engine<_, _> =
            Engine::new_with_state(state, exit_address, current_account_id, io, env);
        let refund_address = args.recipient_address;
        let amount = Wei::new(U256::from_big_endian(&args.amount));
        engine.call(
            &exit_address,
            &refund_address,
            amount,
            Vec::new(),
            u64::MAX,
            vec![
                (exit_address.raw(), Vec::new()),
                (refund_address.raw(), Vec::new()),
            ],
            Vec::new(),
            handler,
        )
    }
```

**File:** engine/src/engine.rs (L1294-1296)
```rust
    if !refund.is_zero() {
        add_balance(io, sender, refund)?;
    }
```

**File:** engine/src/contract_methods/connector.rs (L231-239)
```rust
        } else if let Some(args) = args.refund {
            // Exit call failed; need to refund tokens
            let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;

            if !refund_result.status.is_ok() {
                return Err(errors::ERR_REFUND_FAILURE.into());
            }

            Some(refund_result)
```

**File:** engine-precompiles/src/native.rs (L450-454)
```rust
            #[cfg(feature = "error_refund")]
            refund: refund_call_args(&exit_to_near_params, &exit_event),
            #[cfg(not(feature = "error_refund"))]
            refund: None,
            transfer_near: transfer_near_args,
```
