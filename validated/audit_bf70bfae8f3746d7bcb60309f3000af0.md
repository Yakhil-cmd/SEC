### Title
Permanent Fund Freeze in `ExitToEthereum` Precompile Due to Missing Error-Refund Callback — (File: `engine-precompiles/src/native.rs`)

---

### Summary

The `ExitToEthereum` precompile burns a user's ERC-20 tokens (or ETH) on Aurora before scheduling an asynchronous `withdraw` promise on the eth-connector. Unlike the `ExitToNear` precompile, which attaches an `exit_to_near_precompile_callback` that re-mints tokens when the downstream call fails, `ExitToEthereum` schedules only a bare `PromiseArgs::Create` with no error callback. If the `withdraw` promise is rejected for any reason (e.g., withdrawal is paused on the eth-connector), the tokens are permanently destroyed with no recovery path for the user.

---

### Finding Description

**Burn-before-promise ordering in `EvmErc20.sol`**

`EvmErc20.withdrawToEthereum()` burns the caller's tokens first, then calls the `ExitToEthereum` precompile via an inline assembly `call`:

```solidity
// etc/eth-contracts/contracts/EvmErc20.sol  lines 65-76
function withdrawToEthereum(address recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);          // ← tokens destroyed here
    bytes32 amount_b = bytes32(amount);
    bytes20 recipient_b = bytes20(recipient);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient_b);
    uint input_size = 1 + 32 + 20;
    assembly {
        let res := call(gas(), 0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab, 0,
                        add(input, 32), input_size, 0, 32)
        // res is never checked; no revert on failure
    }
}
```

The assembly block does not check `res` and does not revert, so even if the precompile call returns 0, the burn is not rolled back.

**`ExitToEthereum` precompile schedules no error callback**

Inside `ExitToEthereum::run()`, after parsing the recipient address and building the `withdraw` call arguments, the promise is scheduled as a plain `Create` with no callback:

```rust
// engine-precompiles/src/native.rs  lines 977-990
let withdraw_promise = PromiseCreateArgs {
    target_account_id: nep141_address,
    method: "withdraw".to_string(),
    args: serialized_args,
    attached_balance: Yocto::new(1),
    attached_gas: costs::WITHDRAWAL_GAS,
};

let promise = borsh::to_vec(&PromiseArgs::Create(withdraw_promise)).unwrap();
```

There is no `PromiseArgs::Callback` wrapping this promise, and no `exit_to_ethereum_precompile_callback` method exists anywhere in the codebase.

**Contrast with `ExitToNear`**

`ExitToNear` explicitly attaches a callback that carries `RefundCallArgs` and re-mints burned tokens (or returns ETH) when the `ft_transfer` promise fails:

```rust
// engine-precompiles/src/native.rs  lines 449-483
let callback_args = ExitToNearPrecompileCallbackArgs {
    #[cfg(feature = "error_refund")]
    refund: refund_call_args(&exit_to_near_params, &exit_event),
    ...
};
let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
    PromiseArgs::Create(transfer_promise)
} else {
    PromiseArgs::Callback(PromiseWithCallbackArgs {
        base: transfer_promise,
        callback: PromiseCreateArgs {
            method: "exit_to_near_precompile_callback".to_string(),
            ...
        },
    })
};
```

The callback is handled in `engine/src/contract_methods/connector.rs` (`exit_to_near_precompile_callback`, lines 196-245), which calls `engine::refund_on_error` to re-mint ERC-20 tokens or return ETH. No equivalent exists for `ExitToEthereum`.

**Withdrawal can be paused**

The eth-connector exposes a `PAUSE_WITHDRAW` flag (`engine-tests-connector/src/utils.rs`, line 28: `pub const PAUSE_WITHDRAW: PausedMask = 1 << 1`). When set, any `withdraw` call to the eth-connector will be rejected. A user who calls `withdrawToEthereum` while this flag is active will have their tokens burned and the withdrawal silently dropped.

---

### Impact Explanation

**Critical — Permanent freezing of funds.**

When the eth-connector's `withdraw` method fails (paused, insufficient storage, or any other runtime rejection), the ERC-20 tokens have already been burned on Aurora and there is no on-chain mechanism to recover them. The user suffers a total, irreversible loss of the withdrawn amount. This matches the "permanent freezing of funds" impact class.

---

### Likelihood Explanation

**Medium-High.** The `PAUSE_WITHDRAW` flag is an intentional administrative control that can be activated at any time (e.g., during an incident response). Any user who submits a `withdrawToEthereum` transaction in the same block or NEAR receipt batch as a pause activation will lose funds. Additionally, the eth-connector `withdraw` can fail for other reasons (e.g., the connector account runs out of storage, or a cross-contract call gas exhaustion). Because the burn is irreversible and no callback exists, every such failure is a permanent loss. The attack surface is every holder of a bridged ERC-20 token or ETH on Aurora who attempts to exit to Ethereum.

---

### Recommendation

Add an error-refund callback to `ExitToEthereum` mirroring the existing `exit_to_near_precompile_callback` pattern:

1. Change the scheduled promise from `PromiseArgs::Create` to `PromiseArgs::Callback`, attaching a new `exit_to_ethereum_precompile_callback` method on the Aurora engine contract.
2. In that callback, check `handler.promise_result(0)`. If the result is not `Successful`, call `engine::refund_on_error` with the original sender address and amount to re-mint the burned ERC-20 tokens (or return ETH from the precompile address).
3. Pass the refund address and amount through `ExitToEthereumPrecompileCallbackArgs` (analogous to `ExitToNearPrecompileCallbackArgs`).
4. Guard the feature behind a compile-time flag (e.g., `error_refund`) consistent with the existing pattern, so the callback is a no-op when the feature is disabled but the promise structure is preserved.

---

### Proof of Concept

1. Admin calls `set_paused_flags` on the eth-connector with `PAUSE_WITHDRAW` set.
2. User calls `withdrawToEthereum(recipient, 1_000_000)` on a deployed `EvmErc20` contract.
3. `_burn(msg.sender, 1_000_000)` executes — tokens are destroyed on Aurora.
4. `ExitToEthereum::run()` schedules `PromiseArgs::Create { method: "withdraw", ... }` targeting the eth-connector.
5. The NEAR runtime executes the `withdraw` call; the eth-connector rejects it because `PAUSE_WITHDRAW` is active.
6. No callback fires. The Aurora EVM state is not updated. The 1,000,000 tokens are gone.
7. The user has lost their funds permanently with no on-chain recourse.

**Relevant code locations:**

- Burn without revert guard: [1](#0-0) 
- `ExitToEthereum` bare `Create` promise (no callback): [2](#0-1) 
- `ExitToNear` callback pattern (the missing analog): [3](#0-2) 
- `exit_to_near_precompile_callback` refund logic: [4](#0-3) 
- `PAUSE_WITHDRAW` flag confirming withdrawal can be paused: [5](#0-4) 
- `refund_on_error` re-mint implementation: [6](#0-5)

### Citations

**File:** etc/eth-contracts/contracts/EvmErc20.sol (L65-76)
```text
    function withdrawToEthereum(address recipient, uint256 amount) external override {
        _burn(_msgSender(), amount);

        bytes32 amount_b = bytes32(amount);
        bytes20 recipient_b = bytes20(recipient);
        bytes memory input = abi.encodePacked("\x01", amount_b, recipient_b);
        uint input_size = 1 + 32 + 20;

        assembly {
            let res := call(gas(), 0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab, 0, add(input, 32), input_size, 0, 32)
        }
    }
```

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

**File:** engine-precompiles/src/native.rs (L977-990)
```rust
        let withdraw_promise = PromiseCreateArgs {
            target_account_id: nep141_address,
            method: "withdraw".to_string(),
            args: serialized_args,
            attached_balance: Yocto::new(1),
            attached_gas: costs::WITHDRAWAL_GAS,
        };

        let promise = borsh::to_vec(&PromiseArgs::Create(withdraw_promise)).unwrap();
        let promise_log = Log {
            address: exit_to_ethereum::ADDRESS.raw(),
            topics: Vec::new(),
            data: promise,
        };
```

**File:** engine/src/contract_methods/connector.rs (L196-245)
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
```

**File:** engine-tests-connector/src/utils.rs (L27-28)
```rust
/// Admin control flow flag indicates that withdrawal is paused.
pub const PAUSE_WITHDRAW: PausedMask = 1 << 1;
```

**File:** engine/src/engine.rs (L1176-1224)
```rust
pub fn refund_on_error<I: IO + Copy, E: Env, P: PromiseHandler>(
    io: I,
    env: &E,
    state: EngineState,
    args: &RefundCallArgs,
    handler: &mut P,
) -> EngineResult<SubmitResult> {
    let current_account_id = env.current_account_id();
    if let Some(erc20_address) = args.erc20_address {
        // ERC-20 exit; re-mint burned tokens
        let erc20_admin_address = current_address(&current_account_id);
        let mut engine: Engine<_, _> =
            Engine::new_with_state(state, erc20_admin_address, current_account_id, io, env);

        let refund_address = args.recipient_address;
        let amount = U256::from_big_endian(&args.amount);
        let input = setup_refund_on_error_input(amount, refund_address);

        engine.call(
            &erc20_admin_address,
            &erc20_address,
            Wei::zero(),
            input,
            u64::MAX,
            Vec::new(),
            Vec::new(),
            handler,
        )
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
