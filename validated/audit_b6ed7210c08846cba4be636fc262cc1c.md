### Title
ERC-20 Mirror Token Permanent Freeze on Failed NEP-141 Transfer When `error_refund` Feature Is Disabled — (File: `engine-precompiles/src/native.rs`)

---

### Summary

When the `error_refund` compile-time feature is disabled, the `ExitToNear` precompile creates **no callback promise** for regular ERC-20 exits. If the downstream NEP-141 `ft_transfer` call fails, the ERC-20 tokens already burned in the EVM are permanently lost with no refund path, creating a permanent fund freeze for the user. This is a direct analog to the `recover_token()` accounting omission: a token-exit path that burns supply on one side of the bridge without any mechanism to reconcile the accounting when the cross-chain leg fails.

---

### Finding Description

In `engine-precompiles/src/native.rs`, the `ExitToNear::run` function constructs `ExitToNearPrecompileCallbackArgs` conditionally on the `error_refund` feature flag: [1](#0-0) 

```rust
let callback_args = ExitToNearPrecompileCallbackArgs {
    #[cfg(feature = "error_refund")]
    refund: refund_call_args(&exit_to_near_params, &exit_event),
    #[cfg(not(feature = "error_refund"))]
    refund: None,
    transfer_near: transfer_near_args,
};
```

Then the promise type is chosen: [2](#0-1) 

```rust
let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
    PromiseArgs::Create(transfer_promise)   // ← no callback
} else {
    PromiseArgs::Callback(PromiseWithCallbackArgs { ... })
};
```

For a regular ERC-20 exit (not wNEAR unwrap) with `error_refund` disabled, `callback_args` equals `ExitToNearPrecompileCallbackArgs::default()` — both `refund` and `transfer_near` are `None` — so the bare `PromiseArgs::Create` branch is taken. **No `exit_to_near_precompile_callback` is scheduled.**

The ERC-20 contract (`EvmErc20.sol`) burns tokens unconditionally before calling the precompile: [3](#0-2) 

```solidity
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);   // ← irreversible burn
    ...
    // calls exitToNear precompile
}
```

The full failure sequence:
1. User calls `withdrawToNear(recipient, amount)` on the ERC-20 contract.
2. `_burn` permanently reduces the ERC-20 total supply.
3. The precompile emits a bare `ft_transfer` promise to the NEP-141 contract.
4. `ft_transfer` fails (e.g., recipient not registered, NEP-141 paused).
5. No callback fires; no refund is issued.
6. ERC-20 tokens are gone; NEP-141 tokens remain locked in Aurora's account.

The `EvmErc20.sol` contract also does not encode the sender address into the precompile input (unlike `EvmErc20V2.sol`): [4](#0-3) 

```solidity
bytes memory input = abi.encodePacked("\x01", sender, amount_b, recipient);
``` [3](#0-2) 

```solidity
bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
```

Because the sender is absent from the precompile input, even if `error_refund` is later enabled at the engine level, ERC-20 contracts already deployed with `EvmErc20.sol` cannot be refunded — the refund address is irrecoverably missing from the calldata.

The `refund_on_error` function in `engine/src/engine.rs` that would re-mint burned ERC-20 tokens is never reached: [5](#0-4) 

And the callback handler in `engine/src/contract_methods/connector.rs` confirms the silent no-op when `refund` is `None` and the promise failed: [6](#0-5) 

```rust
} else if let Some(args) = args.refund {
    let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;
    Some(refund_result)
} else {
    None   // ← silent no-op: tokens permanently lost
};
```

### Citations

**File:** engine-precompiles/src/native.rs (L449-455)
```rust
        let callback_args = ExitToNearPrecompileCallbackArgs {
            #[cfg(feature = "error_refund")]
            refund: refund_call_args(&exit_to_near_params, &exit_event),
            #[cfg(not(feature = "error_refund"))]
            refund: None,
            transfer_near: transfer_near_args,
        };
```

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

**File:** etc/eth-contracts/contracts/EvmErc20.sol (L53-60)
```text
    function withdrawToNear(bytes memory recipient, uint256 amount) external override {
        _burn(_msgSender(), amount);

        bytes32 amount_b = bytes32(amount);
        bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
        uint input_size = 1 + 32 + recipient.length;

        assembly {
```

**File:** etc/eth-contracts/contracts/EvmErc20V2.sol (L53-64)
```text
    function withdrawToNear(bytes memory recipient, uint256 amount) external override {
        address sender = _msgSender();
        _burn(sender, amount);

        bytes32 amount_b = bytes32(amount);
        bytes memory input = abi.encodePacked("\x01", sender, amount_b, recipient);
        uint input_size = 1 + 20 + 32 + recipient.length;

        assembly {
            let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        }
    }
```

**File:** engine/src/engine.rs (L1176-1204)
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
```

**File:** engine/src/contract_methods/connector.rs (L231-242)
```rust
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
```
