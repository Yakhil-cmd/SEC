### Title
Permanent Token Loss on Failed `ExitToNear` Promise When `error_refund` Feature Is Absent - (File: `engine-precompiles/src/native.rs`, `etc/eth-contracts/contracts/EvmErc20.sol`)

### Summary

The `ExitToNear` precompile burns ERC-20 tokens (or deducts ETH) synchronously during EVM execution, then schedules an asynchronous NEAR `ft_transfer` promise. If that promise fails and the `error_refund` compile-time feature is not enabled, no refund callback is registered and the burned tokens are permanently lost. Additionally, `EvmErc20.withdrawToNear` never checks the return value of the assembly `call` to the precompile, so a synchronous precompile failure also silently destroys tokens.

---

### Finding Description

**Step 1 — Tokens are burned before the async promise resolves.**

`EvmErc20.withdrawToNear` burns the caller's tokens first, then calls the `ExitToNear` precompile:

```solidity
// etc/eth-contracts/contracts/EvmErc20.sol  lines 53-63
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);          // ← tokens destroyed here, synchronously

    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    uint input_size = 1 + 32 + recipient.length;

    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f,
                        0, add(input, 32), input_size, 0, 32)
        // res is NEVER checked
    }
}
``` [1](#0-0) 

The `res` return value of the `call` opcode is stored but never inspected. Under standard EVM semantics a failed inner `call` returns 0 without reverting the outer frame, so a precompile failure leaves `_burn` committed and no exit scheduled.

**Step 2 — The refund callback is gated behind a compile-time feature.**

Inside the precompile, the `refund` field of `ExitToNearPrecompileCallbackArgs` is populated only when the `error_refund` feature is compiled in:

```rust
// engine-precompiles/src/native.rs  lines 449-455
let callback_args = ExitToNearPrecompileCallbackArgs {
    #[cfg(feature = "error_refund")]
    refund: refund_call_args(&exit_to_near_params, &exit_event),
    #[cfg(not(feature = "error_refund"))]
    refund: None,                          // ← no refund path compiled in
    transfer_near: transfer_near_args,
};
``` [2](#0-1) 

When `callback_args` equals the default (i.e., `refund: None` and `transfer_near: None`), only a bare `PromiseArgs::Create` is emitted — no callback is attached:

```rust
// engine-precompiles/src/native.rs  lines 470-483
let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
    PromiseArgs::Create(transfer_promise)   // ← no callback, no refund possible
} else {
    PromiseArgs::Callback(PromiseWithCallbackArgs { ... })
};
``` [3](#0-2) 

**Step 3 — The callback itself only refunds when `args.refund` is `Some`.**

```rust
// engine/src/contract_methods/connector.rs  lines 231-239
} else if let Some(args) = args.refund {
    // Exit call failed; need to refund tokens
    let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;
    ...
} else {
    None   // ← no refund, no error, tokens gone
};
``` [4](#0-3) 

**Step 4 — The test suite explicitly confirms the loss.**

```rust
// engine-tests/src/tests/erc20_connector.rs  lines 656-660
#[cfg(feature = "error_refund")]
let balance = FT_TRANSFER_AMOUNT.into();
// If the refund feature is not enabled then there is no refund in the EVM
#[cfg(not(feature = "error_refund"))]
let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();
``` [5](#0-4) 

The same pattern is confirmed for ETH exits at lines 771–775. [6](#0-5) 

---

### Impact Explanation

When the `error_refund` feature is absent from the production build, any user who calls `withdrawToNear` and whose downstream `ft_transfer` promise fails (e.g., recipient account not registered with the NEP-141 contract, NEP-141 contract paused, insufficient storage deposit on the recipient) permanently loses the full exit amount. The ERC-20 tokens are burned; no NEP-141 tokens arrive; no re-mint occurs. This is **permanent freezing / destruction of user funds**.

The same loss occurs for ETH exits via the base-token path when the `ft_transfer` to the ETH connector fails.

---

### Likelihood Explanation

The failure condition — a recipient NEAR account that has not called `storage_deposit` on the NEP-141 contract — is a routine user mistake that is well-documented in the NEAR ecosystem. Any user who supplies an unregistered recipient account ID triggers the loss. No privileged access is required; the entry point is the standard ERC-20 `withdrawToNear` function callable by any token holder. The `error_refund` feature is a compile-time opt-in, meaning deployments that omit it silently expose all users to this risk with no on-chain indication.

---

### Recommendation

1. **Always check the precompile return value in `EvmErc20.withdrawToNear`.** Revert if `res == 0`:
   ```solidity
   assembly {
       let res := call(...)
       if iszero(res) { revert(0, 0) }
   }
   ```
   This prevents the burn from being committed when the precompile itself fails synchronously.

2. **Make `error_refund` the unconditional default** (remove the `#[cfg]` gate or always set `refund` to `Some`). The refund path in `exit_to_near_precompile_callback` is the only safety net against async `ft_transfer` failures; it must always be present.

3. **Document the recipient registration requirement** prominently in the exit flow so users know to call `storage_deposit` on the target NEP-141 contract before initiating a withdrawal.

---

### Proof of Concept

1. Alice holds 1000 units of a bridged ERC-20 token on Aurora (production build compiled without `error_refund`).
2. Alice calls `erc20.withdrawToNear("alice-unregistered.near", 1000)`.
3. `_burn(alice, 1000)` executes — her ERC-20 balance drops to 0.
4. The `ExitToNear` precompile schedules `ft_transfer` on the NEP-141 contract targeting `alice-unregistered.near`.
5. The NEP-141 `ft_transfer` fails because `alice-unregistered.near` has no storage deposit.
6. Because `refund: None` was compiled in, no `exit_to_near_precompile_callback` with a refund is scheduled.
7. Alice's 1000 ERC-20 tokens are permanently destroyed; she receives 0 NEP-141 tokens. [1](#0-0) [2](#0-1) [4](#0-3)

### Citations

**File:** etc/eth-contracts/contracts/EvmErc20.sol (L53-63)
```text
    function withdrawToNear(bytes memory recipient, uint256 amount) external override {
        _burn(_msgSender(), amount);

        bytes32 amount_b = bytes32(amount);
        bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
        uint input_size = 1 + 32 + recipient.length;

        assembly {
            let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        }
    }
```

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

**File:** engine-tests/src/tests/erc20_connector.rs (L656-660)
```rust
        #[cfg(feature = "error_refund")]
        let balance = FT_TRANSFER_AMOUNT.into();
        // If the refund feature is not enabled then there is no refund in the EVM
        #[cfg(not(feature = "error_refund"))]
        let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();
```

**File:** engine-tests/src/tests/erc20_connector.rs (L771-775)
```rust
        #[cfg(feature = "error_refund")]
        let expected_balance = Wei::new_u64(INITIAL_ETH_BALANCE);
        // If the refund feature is not enabled, then there is no refund in the EVM
        #[cfg(not(feature = "error_refund"))]
        let expected_balance = Wei::new_u64(INITIAL_ETH_BALANCE - ETH_EXIT_AMOUNT);
```
