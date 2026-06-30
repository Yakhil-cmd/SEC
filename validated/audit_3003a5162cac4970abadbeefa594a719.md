### Title
ERC-20 Tokens Permanently Burned When NEP-141 `ft_transfer` Fails in `ExitToNear` Precompile Without `error_refund` - (File: `engine-precompiles/src/native.rs`)

### Summary
The `ExitToNear` precompile irreversibly burns ERC-20 tokens inside the EVM before the asynchronous NEP-141 `ft_transfer` promise executes. When the `error_refund` feature is not compiled in, any failure of the `ft_transfer` — including failure caused by a minimum-balance constraint, a pause flag, or an unregistered recipient enforced by the NEP-141 token contract — results in the ERC-20 tokens being permanently destroyed while the corresponding NEP-141 tokens remain locked inside the Aurora engine account with no recovery path.

### Finding Description
In `ExitToNear::run` (`engine-precompiles/src/native.rs`), the EVM execution first burns the caller's ERC-20 tokens (this is the irreversible on-chain state change inside the EVM). The precompile then constructs a NEAR promise to call `ft_transfer` on the NEP-141 contract. Whether a callback is attached depends on the compile-time `error_refund` feature:

```rust
// engine-precompiles/src/native.rs lines 449-483
let callback_args = ExitToNearPrecompileCallbackArgs {
    #[cfg(feature = "error_refund")]
    refund: refund_call_args(&exit_to_near_params, &exit_event),
    #[cfg(not(feature = "error_refund"))]
    refund: None,
    transfer_near: transfer_near_args,
};

let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
    PromiseArgs::Create(transfer_promise)   // ← NO callback attached
} else {
    PromiseArgs::Callback(PromiseWithCallbackArgs { ... })
};
```

For a standard ERC-20 exit (not wNEAR unwrap) without `error_refund`:
- `callback_args.refund = None`
- `callback_args.transfer_near = None`
- `callback_args == ExitToNearPrecompileCallbackArgs::default()` → **TRUE**
- The promise is `PromiseArgs::Create(transfer_promise)` — **no callback, no refund path**

NEAR promises execute asynchronously in a subsequent block. Any NEP-141 token can enforce transfer-time constraints analogous to the `minUSTokens` pattern in the external report:
- A minimum-balance rule preventing the sender (Aurora engine) from going below a threshold after the transfer
- A pause flag set by the token admin
- A storage-registration requirement for the recipient

If any such constraint causes `ft_transfer` to revert, the ERC-20 tokens are already burned and there is no callback to issue a refund. The NEP-141 tokens remain in the Aurora engine account, inaccessible to the user.

The `exit_to_near_precompile_callback` handler in `engine/src/contract_methods/connector.rs` (lines 196–245) only executes when a callback was attached; without `error_refund` it is never scheduled for a plain ERC-20 exit, so the failure is silently swallowed.

The existing test `test_exit_to_near_refund` in `engine-tests/src/tests/erc20_connector.rs` (lines 623–666) explicitly documents this loss:

```rust
#[cfg(feature = "error_refund")]
let balance = FT_TRANSFER_AMOUNT.into();
// If the refund feature is not enabled then there is no refund in the EVM
#[cfg(