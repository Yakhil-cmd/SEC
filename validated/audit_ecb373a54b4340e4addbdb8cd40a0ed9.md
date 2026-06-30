### Title
Permanent Fund Freeze in `ExitToEthereum` Precompile Due to Missing Callback/Refund on Failed `withdraw` Promise - (File: engine-precompiles/src/native.rs)

---

### Summary

The `ExitToEthereum` precompile burns ERC-20 tokens atomically inside the EVM execution, then schedules a single NEAR promise to call `withdraw` on the connector contract — with no callback and no refund path. If that promise fails for any reason (e.g., connector NEP-141 accounting discrepancy, insufficient balance on the NEAR side), the ERC-20 tokens are permanently destroyed and the user receives nothing on Ethereum. Unlike `ExitToNear`, which has an `exit_to_near_precompile_callback` refund path, `ExitToEthereum` has no recovery mechanism whatsoever.

---

### Finding Description

In `ExitToEthereum::run()`, after the ERC-20 burn is committed inside the EVM, the precompile constructs a single fire-and-forget NEAR promise:

```rust
// engine-precompiles/src/native.rs  lines 977–985
let withdraw_promise = PromiseCreateArgs {
    target_account_id: nep141_address,
    method: "withdraw".to_string(),
    args: serialized_args,
    attached_balance: Yocto::new(1),
    attached_gas: costs::WITHDRAWAL_GAS,
};

let promise = borsh::to_vec(&PromiseArgs::Create(withdraw_promise)).unwrap();
```

`PromiseArgs::Create` schedules exactly one promise with no attached callback. The EVM execution (including the ERC-20 burn) is committed before this promise executes. If the NEAR-side `withdraw` call fails, NEAR does not roll back the already-committed EVM state.

Contrast this with `ExitToNear`, which conditionally wraps the outbound promise in a `PromiseArgs::Callback` that invokes `exit_to_near_precompile_callback` on the engine, allowing a refund:

```rust
// engine-precompiles/src/native.rs  lines 470–483
let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
    PromiseArgs::Create(transfer_promise)
} else {
    PromiseArgs::Callback(PromiseWithCallbackArgs {
        base: transfer_promise,
        callback: PromiseCreateArgs {
            target_account_id: self.current_account_id.clone(),
            method: "exit_to_near_precompile_callback".to_string(),
            ...
        },
    })
};
```

`ExitToEthereum` has no equivalent. The asymmetry is structural and unconditional — it does not depend on any compile-time feature flag.

The `withdraw` call on the connector contract can fail when the connector's NEP-141 balance attributed to the Aurora engine account is less than the amount being withdrawn. This is the direct analog of the Morpho "hard withdraw" failing because the pool borrow step is blocked by a threshold condition: in Morpho the position sits between max-LTV and liquidation threshold; in Aurora the connector's NEP-141 balance sits below the requested withdrawal amount. Both represent a valid-looking user action that is blocked at an intermediate step, with the prior destructive step (ERC-20 burn / P2P position reduction) already committed and irreversible.

A NEP-141 balance shortfall on the connector side can arise from:
- A prior failed or partially-applied bridge transaction that burned ERC-20 tokens on the EVM side but did not complete the corresponding NEP-141 mint on the NEAR side, leaving the connector's engine-attributed balance lower than the EVM ERC-20 total supply.
- Any bridge accounting discrepancy introduced by a bug in the connector contract or the bridge relayer.

Once the shortfall exists, every subsequent `ExitToEthereum` call that exceeds the remaining connector balance will silently burn the caller's ERC-20 tokens with no Ethereum withdrawal and no refund.

---

### Impact Explanation

**Critical — Permanent freezing of funds.**

The ERC-20 tokens are burned inside the EVM execution, which is committed to NEAR storage before the `withdraw` promise runs. If `withdraw` fails, the tokens are gone: they no longer exist in the EVM, and no Ethereum-side withdrawal record is created. There is no callback, no refund, and no retry path. The loss is permanent.

---

### Likelihood Explanation

**Low-to-Medium.** Normal operation keeps the connector's NEP-141 balance in sync with the EVM ERC-20 supply. However, any bridge accounting discrepancy — whether from a prior failed transaction, a connector bug, or a partial bridge failure — creates a persistent shortfall. Once that shortfall exists, it is not self-healing: every `ExitToEthereum` call that hits the shortfall permanently destroys the caller's tokens. The condition is not transient; it accumulates until the shortfall is manually corrected by an operator.

---

### Recommendation

Add a callback to the `ExitToEthereum` promise, mirroring the `exit_to_near_precompile_callback` pattern already present in `ExitToNear`. The callback should:
1. Check the promise result.
2. If `withdraw` failed, re-mint the ERC-20 tokens to the original sender (or credit the equivalent ETH balance) so the user's funds are not permanently lost.

The callback infrastructure already exists in the engine (`exit_to_near_precompile_callback` in `engine/src/contract_methods/connector.rs` lines 196–246); a parallel `exit_to_ethereum_precompile_callback` should be introduced.

---

### Proof of Concept

**Setup:** Suppose a prior bridge failure has left the connector's NEP-141 balance for the Aurora engine 1 ETH short of the EVM ERC-20 total supply.

**Attack / trigger path (unprivileged user):**

1. Alice holds 1 ETH worth of the bridged ERC-20 token on Aurora.
2. Alice calls the `ExitToEthereum` precompile (flag `0x0` for ETH base token, or `0x1` for ERC-20) with her full balance.
3. Inside the EVM execution, Alice's ERC-20 tokens are burned. The EVM state is committed to NEAR storage.
4. The engine schedules `PromiseArgs::Create(withdraw_promise)` targeting the connector's `withdraw` method.
5. The connector's `withdraw` call fails because the engine's NEP-141 balance is insufficient to cover Alice's withdrawal.
6. NEAR executes no callback (none was registered). The failed promise result is discarded.
7. Alice's ERC-20 tokens are permanently gone. No Ethereum withdrawal is initiated. No refund is issued.

**Relevant code locations:**

- Burn + promise creation (no callback): [1](#0-0) 
- `ExitToNear` callback path (the missing analog): [2](#0-1) 
- Callback handler that `ExitToEthereum` lacks: [3](#0-2)

### Citations

**File:** engine-precompiles/src/native.rs (L470-483)
```rust
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

**File:** engine-precompiles/src/native.rs (L977-985)
```rust
        let withdraw_promise = PromiseCreateArgs {
            target_account_id: nep141_address,
            method: "withdraw".to_string(),
            args: serialized_args,
            attached_balance: Yocto::new(1),
            attached_gas: costs::WITHDRAWAL_GAS,
        };

        let promise = borsh::to_vec(&PromiseArgs::Create(withdraw_promise)).unwrap();
```

**File:** engine/src/contract_methods/connector.rs (L196-246)
```rust
pub fn exit_to_near_precompile_callback<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<Option<SubmitResult>, ContractError> {
    with_hashchain(io, env, function_name!(), |io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;
        env.assert_private_call()?;

        // This function should only be called as the callback of
        // exactly one promise.
        if handler.promise_results_count() != 1 {
            return Err(errors::ERR_PROMISE_COUNT.into());
        }

        let args: ExitToNearPrecompileCallbackArgs = io.read_input_borsh()?;

        let maybe_result = if let Some(PromiseResult::Successful(_)) = handler.promise_result(0) {
            if let Some(args) = args.transfer_near {
                let action = PromiseAction::Transfer {
                    amount: Yocto::new(args.amount),
                };
                let promise = PromiseBatchAction {
                    target_account_id: args.target_account_id,
                    actions: vec![action],
                };

                // Safety: this call is safe because it comes from the exit to near precompile, not users.
                // The call is to transfer the unwrapped wNEAR tokens.
                let promise_id = handler.promise_create_batch(&promise);
                handler.promise_return(promise_id);
            }

            None
        } else if let Some(args) = args.refund {
            // Exit call failed; need to refund tokens
            let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;

            if !refund_result.status.is_ok() {
                return Err(errors::ERR_REFUND_FAILURE.into());
            }

            Some(refund_result)
        } else {
            None
        };

        Ok(maybe_result)
    })
}
```
