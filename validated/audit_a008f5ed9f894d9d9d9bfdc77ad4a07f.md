### Title
ERC-20 Tokens Permanently Burned Without Confirmed NEAR-Side Transfer When `error_refund` Feature Is Absent - (File: `engine-precompiles/src/native.rs`)

### Summary

When a user calls `withdrawToNear()` on an `EvmErc20` contract, their ERC-20 tokens are burned synchronously inside the EVM before the asynchronous NEAR-side `ft_transfer` promise is confirmed. Without the `error_refund` compile-time feature, no refund callback is registered. If the NEAR promise fails (e.g., recipient not registered with the NEP-141 contract), the burned ERC-20 tokens are permanently lost with no recovery path.

### Finding Description

The `ExitToNear` precompile in `engine-precompiles/src/native.rs` schedules a NEAR cross-contract call (`ft_transfer` or `ft_transfer_call`) after the ERC-20 burn has already been committed to EVM state. The refund mechanism is gated behind a compile-time feature flag:

```rust
// engine-precompiles/src/native.rs lines 449-453
let callback_args = ExitToNearPrecompileCallbackArgs {
    #[cfg(feature = "error_refund")]
    refund: refund_call_args(&exit_to_near_params, &exit_event),
    #[cfg(not(feature = "error_refund"))]
    refund: None,
    transfer_near: transfer_near_args,
};
```

For the common non-wNEAR ERC-20 exit path (flag `0x01`, no `transfer_near`), `callback_args` equals `ExitToNearPrecompileCallbackArgs::default()` when `error_refund` is absent, so the promise is created with **no callback at all**:

```rust
// engine-precompiles/src/native.rs lines 470-483
let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
    PromiseArgs::Create(transfer_promise)   // ← no callback registered
} else {
    PromiseArgs::Callback(PromiseWithCallbackArgs { ... })
};
```

Even in the wNEAR unwrap path where a callback is registered, `exit_to_near_precompile_callback` silently returns `Ok(None)` when the promise fails and `args.refund` is `None`:

```rust
// engine/src/contract_methods/connector.rs lines 231-242
} else if let Some(args) = args.refund {
    let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;
    ...
} else {
    None   // ← promise failed, refund is None → tokens silently lost
};
```

The ERC-20 contract (`EvmErc20.sol`) burns tokens before calling the precompile:

```solidity
// etc/eth-contracts/contracts/EvmErc20.sol lines 53-62
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);   // ← burn happens first, unconditionally
    // ... calls ExitToNear precompile
}
```

The engine itself selects which ERC-20 binary to deploy based on the same feature flag:

```rust
// engine/src/engine.rs lines 1321-1324
#[cfg(feature = "error_refund")]
let erc20_contract = include_bytes!("../../etc/eth-contracts/res/EvmErc20V2.bin");
#[cfg(not(feature = "error_refund"))]
let erc20_contract = include_bytes!("../../etc/eth-contracts/res/EvmErc20.bin");
```

The test suite explicitly acknowledges the loss:

```rust
// engine-tests/src/tests/erc20_connector.rs lines 656-660
#[cfg(feature = "error_refund")]
let balance = FT_TRANSFER_AMOUNT.into();
// If the refund feature is not enabled then there is no refund in the EVM
#[cfg(not(feature = "error_refund"))]
let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();
```

### Impact Explanation

Any user who calls `withdrawToNear()` on a bridged ERC-20 token when the downstream NEAR `ft_transfer` fails will have their ERC-20 tokens permanently burned with zero NEP-141 tokens received. The EVM state is committed (burn is final), the NEAR-side transfer never completes, and no refund path exists. This constitutes **permanent freezing/destruction of user funds** — a Critical impact.

### Likelihood Explanation

The failure condition is reachable by any unprivileged EVM user. Common triggers include: sending to a NEAR account not registered with the NEP-141 contract, the NEP-141 contract being paused, or insufficient attached gas causing the NEAR promise to fail. No special privileges are required. The user only needs to call `withdrawToNear()` with a recipient that causes the NEAR-side `ft_transfer` to revert.

### Recommendation

The refund mechanism must not be gated behind a compile-time feature flag for production deployments. The `ExitToNearPrecompileCallbackArgs` should always carry a populated `refund` field for any exit that burns tokens, and `exit_to_near_precompile_callback` must always attempt a refund when the base promise fails. Specifically:

1. Remove the `#[cfg(feature = "error_refund")]` / `#[cfg(not(feature = "error_refund"))]` conditional on the `refund` field population.
2. Always register a callback promise (never use `PromiseArgs::Create` for token-burning exits).
3. In `exit_to_near_precompile_callback`, treat a failed promise with `refund: None` as an error rather than silently returning `Ok(None)`.

### Proof of Concept

1. Deploy Aurora Engine **without** the `error_refund` feature (uses `EvmErc20.bin`).
2. Bridge a NEP-141 token to Aurora; user receives ERC-20 tokens at address `erc20`.
3. User calls `erc20.withdrawToNear("unregistered.near", amount)`.
4. Inside the EVM: `_burn(user, amount)` executes — ERC-20 balance goes to zero.
5. The `ExitToNear` precompile schedules `ft_transfer("unregistered.near", amount)` as a bare `PromiseArgs::Create` with no callback.
6. The NEAR `ft_transfer` fails because `unregistered.near` has no storage deposit with the NEP-141 contract.
7. No callback fires; no refund is issued.
8. User's ERC-20 balance is zero; NEP-141 balance of `unregistered.near` is zero; tokens are destroyed.

This is confirmed by the test at `engine-tests/src/tests/erc20_connector.rs:656-660` which explicitly documents that without `error_refund`, `FT_EXIT_AMOUNT` tokens are unrecoverable after a failed exit. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** engine-precompiles/src/native.rs (L449-453)
```rust
        let callback_args = ExitToNearPrecompileCallbackArgs {
            #[cfg(feature = "error_refund")]
            refund: refund_call_args(&exit_to_near_params, &exit_event),
            #[cfg(not(feature = "error_refund"))]
            refund: None,
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

**File:** etc/eth-contracts/contracts/EvmErc20.sol (L53-62)
```text
    function withdrawToNear(bytes memory recipient, uint256 amount) external override {
        _burn(_msgSender(), amount);

        bytes32 amount_b = bytes32(amount);
        bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
        uint input_size = 1 + 32 + recipient.length;

        assembly {
            let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        }
```

**File:** engine/src/engine.rs (L1321-1324)
```rust
    #[cfg(feature = "error_refund")]
    let erc20_contract = include_bytes!("../../etc/eth-contracts/res/EvmErc20V2.bin");
    #[cfg(not(feature = "error_refund"))]
    let erc20_contract = include_bytes!("../../etc/eth-contracts/res/EvmErc20.bin");
```

**File:** engine-tests/src/tests/erc20_connector.rs (L656-660)
```rust
        #[cfg(feature = "error_refund")]
        let balance = FT_TRANSFER_AMOUNT.into();
        // If the refund feature is not enabled then there is no refund in the EVM
        #[cfg(not(feature = "error_refund"))]
        let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();
```
