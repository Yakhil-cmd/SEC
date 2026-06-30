### Title
Permanent ETH Freeze via Revert-on-Receive `refund_address` in `ExitToNear` Push-Refund Model — (`engine/src/engine.rs`, `engine/src/contract_methods/connector.rs`)

---

### Summary

When the `error_refund` feature is compiled in, the `ExitToNear` precompile accepts a fully user-controlled `refund_address` (20 bytes from raw calldata). If the `ft_transfer` NEAR promise fails and the callback attempts to push ETH back to that address via an EVM call, a contract at `refund_address` that reverts on receiving ETH will cause `refund_on_error` to return a non-success `SubmitResult`. The callback then returns `ERR_REFUND_FAILURE`, the NEAR callback state is rolled back, and the ETH remains permanently frozen at the `exit_to_near::ADDRESS` precompile address (`0xe9217bc70b7ed1f598ddd3199e80b093fa71124f`) with no recovery path.

---

### Finding Description

**Step 1 — User-controlled `refund_address` is parsed without validation.** [1](#0-0) 

The 20 bytes at `input[1..21]` are taken verbatim as the `refund_address`. Any EVM address — including a contract whose `receive()` reverts — is accepted.

**Step 2 — `ExitToNear::run()` burns/locks ETH and schedules a NEAR `ft_transfer` promise with a callback.** [2](#0-1) 

The ETH is deducted from the caller's EVM balance and credited to `exit_to_near::ADDRESS` as part of the EVM transaction. The NEAR promise and callback are scheduled asynchronously.

**Step 3 — When `ft_transfer` fails, the callback calls `refund_on_error`.** [3](#0-2) 

**Step 4 — `refund_on_error` for ETH exit pushes ETH to `refund_address` via an EVM call (push model).** [4](#0-3) 

This is a plain EVM value-transfer call (`data = Vec::new()`) to `refund_address`. If `refund_address` is a contract with no `receive()` function, or one that reverts, the EVM call returns `TransactionStatus::Revert(...)`.

**Step 5 — A non-success `SubmitResult` causes `ERR_REFUND_FAILURE`, the callback panics, and the ETH is permanently frozen.** [5](#0-4) 

When the NEAR callback panics, its own state changes are rolled back. But the original EVM transaction (which moved ETH to `exit_to_near::ADDRESS`) was committed in a prior NEAR transaction and is not rolled back. The ETH is now permanently stranded at `exit_to_near::ADDRESS` with no admin recovery function.

---

### Impact Explanation

**Critical — Permanent freezing of funds.**

ETH sent through the `ExitToNear` precompile is irrecoverably frozen at `exit_to_near::ADDRESS` whenever:
- The `ft_transfer` NEAR promise fails (e.g., unregistered recipient, insufficient NEP-141 balance), AND
- The `refund_address` is a contract that reverts on receiving ETH.

There is no admin sweep, no timeout withdrawal, and no second-chance refund path. The `exit_to_near::ADDRESS` precompile address has no privileged withdrawal method.

---

### Likelihood Explanation

**Medium.** The `error_refund` feature must be compiled in (it is the production refund mechanism). The attacker must:
1. Deploy a contract on Aurora that reverts on ETH reception (trivial — any contract without a `receive()` or with `revert()` in `receive()`).
2. Call the `ExitToNear` precompile from that contract (or specify it as `refund_address`) with a NEAR recipient that will fail `ft_transfer` (e.g., an unregistered account).

Both conditions are fully attacker-controlled and require no privileged access. The attack is cheap and repeatable.

---

### Recommendation

Replace the push model in `refund_on_error` for the ETH case with a pull model: instead of calling `engine.call(&exit_address, &refund_address, amount, ...)`, credit the `refund_address`'s EVM balance directly using `add_balance` (as `refund_unused_gas` already does). This eliminates the EVM call entirely and removes the revert-on-receive attack surface. [6](#0-5) 

The `add_balance` pattern used in `refund_unused_gas` is the correct pull-model analog.

---

### Proof of Concept

```
1. Deploy MaliciousRefund on Aurora:
   contract MaliciousRefund {
       receive() external payable { revert(); }
   }

2. From MaliciousRefund (or any EOA specifying it as refund_address),
   call ExitToNear precompile (0xe9217bc70b7ed1f598ddd3199e80b093fa71124f)
   with input:
     [0x00]                          // flag: ETH base token
     [MaliciousRefund address bytes] // refund_address (20 bytes)
     [b"unregistered.near"]          // NEAR recipient (not registered with NEP-141)
   and msg.value = N ETH.

3. The EVM transaction succeeds: N ETH moves from caller to exit_to_near::ADDRESS.

4. NEAR ft_transfer to "unregistered.near" fails.

5. exit_to_near_precompile_callback fires; refund_on_error attempts
   engine.call(exit_to_near::ADDRESS → MaliciousRefund, N ETH, data=[]).

6. MaliciousRefund.receive() reverts → TransactionStatus::Revert.

7. !refund_result.status.is_ok() → ERR_REFUND_FAILURE returned.

8. N ETH is permanently frozen at exit_to_near::ADDRESS.
   No recovery path exists.
```

### Citations

**File:** engine-precompiles/src/native.rs (L449-483)
```rust
        let callback_args = ExitToNearPrecompileCallbackArgs {
            #[cfg(feature = "error_refund")]
            refund: refund_call_args(&exit_to_near_params, &exit_event),
            #[cfg(not(feature = "error_refund"))]
            refund: None,
            transfer_near: transfer_near_args,
        };
        let attached_gas = if method == "ft_transfer_call" {
            costs::FT_TRANSFER_CALL_GAS
        } else {
            costs::FT_TRANSFER_GAS
        };

        let transfer_promise = PromiseCreateArgs {
            target_account_id: nep141_address,
            method,
            args: args.into_bytes(),
            attached_balance: Yocto::new(1),
            attached_gas,
        };

        let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
            PromiseArgs::Create(transfer_promise)
        } else {
            PromiseArgs::Callback(PromiseWithCallbackArgs {
                base: transfer_promise,
                callback: PromiseCreateArgs {
                    target_account_id: self.current_account_id.clone(),
                    method: "exit_to_near_precompile_callback".to_string(),
                    args: borsh::to_vec(&callback_args).unwrap(),
                    attached_balance: Yocto::new(0),
                    attached_gas: costs::EXIT_TO_NEAR_CALLBACK_GAS,
                },
            })
        };
```

**File:** engine-precompiles/src/native.rs (L778-785)
```rust
#[cfg(feature = "error_refund")]
fn parse_input(input: &[u8]) -> Result<(Address, &[u8]), ExitError> {
    validate_input_size(input, MIN_INPUT_SIZE, MAX_INPUT_SIZE)?;
    let mut buffer = [0; 20];
    buffer.copy_from_slice(&input[1..21]);
    let refund_address = Address::from_array(buffer);
    Ok((refund_address, &input[21..]))
}
```

**File:** engine/src/contract_methods/connector.rs (L231-237)
```rust
        } else if let Some(args) = args.refund {
            // Exit call failed; need to refund tokens
            let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;

            if !refund_result.status.is_ok() {
                return Err(errors::ERR_REFUND_FAILURE.into());
            }
```

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
