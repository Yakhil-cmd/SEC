### Title
ERC-20 Tokens Permanently Burned With No Refund Path When `ft_transfer` Fails and `error_refund` Feature Is Disabled - (File: engine-precompiles/src/native.rs)

---

### Summary

When the `error_refund` Cargo feature is not enabled (which is the case in the default and `contract` production build), the `ExitToNear` precompile hardcodes `refund: None` in the callback arguments. If the downstream `ft_transfer` NEAR promise fails after ERC-20 tokens have already been burned in the EVM, no refund callback is scheduled and the burned tokens are permanently unrecoverable. This is a direct structural analog to the reported vulnerability: a routing field that controls the refund path is left unset, causing tokens to be locked forever.

---

### Finding Description

The `ExitToNear` precompile's `run` function constructs `ExitToNearPrecompileCallbackArgs` with a `refund` field that is conditionally populated only when the `error_refund` Cargo feature is compiled in:

```rust
// engine-precompiles/src/native.rs, lines 449–455
let callback_args = ExitToNearPrecompileCallbackArgs {
    #[cfg(feature = "error_refund")]
    refund: refund_call_args(&exit_to_near_params, &exit_event),
    #[cfg(not(feature = "error_refund"))]
    refund: None,
    transfer_near: transfer_near_args,
};
``` [1](#0-0) 

When `error_refund` is absent (the default), `refund` is always `None`. For a standard ERC-20 exit (non-wNEAR), `transfer_near` is also `None`. The code then checks:

```rust
// engine-precompiles/src/native.rs, lines 470–483
let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
    PromiseArgs::Create(transfer_promise)   // no callback attached
} else {
    PromiseArgs::Callback(...)
};
``` [2](#0-1) 

Because both fields are `None`, `callback_args` equals `default()`, so only a bare `PromiseArgs::Create(ft_transfer)` is emitted — **no callback is ever attached**. If the `ft_transfer` NEAR promise fails (e.g., the recipient NEAR account has not performed a storage deposit on the NEP-141 contract), the NEAR runtime silently discards the failure. The ERC-20 tokens that were already burned in the EVM by `EvmErc20.withdrawToNear` are gone with no re-mint path.

The `EvmErc20.sol` contract burns tokens unconditionally before calling the precompile:

```solidity
// etc/eth-contracts/contracts/EvmErc20.sol, lines 53–62
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);
    ...
    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, ...)
    }
}
``` [3](#0-2) 

The `error_refund` feature is **not** present in the `default` or `contract` feature sets of either the engine or precompiles crate:

```toml
# engine-precompiles/Cargo.toml, lines 34–39
[features]
default = ["std"]
...
error_refund = []
``` [4](#0-3) 

```toml
# engine/Cargo.toml, lines 42–50
[features]
default = ["std"]
contract = ["log", "aurora-engine-sdk/contract", "aurora-engine-precompiles/contract"]
...
error_refund = ["aurora-engine-precompiles/error_refund"]
``` [5](#0-4) 

The production WASM is built with the `contract` feature, which does not pull in `error_refund`. The refund routing field is therefore structurally equivalent to the uninitialized `tokenDistribution` variable in the reported issue: it is always `None`, the wrong code path (no-callback) is always taken, and tokens are permanently lost on failure.

The test suite explicitly acknowledges this behavior:

```rust
// engine-tests/src/tests/erc20_connector.rs, lines 656–660
#[cfg(feature = "error_refund")]
let balance = FT_TRANSFER_AMOUNT.into();
// If the refund feature is not enabled then there is no refund in the EVM
#[cfg(not(feature = "error_refund"))]
let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();
``` [6](#0-5) 

---

### Impact Explanation

**Critical — Permanent freezing of funds.**

When a user calls `withdrawToNear` on any bridged ERC-20 (`EvmErc20`) and the downstream `ft_transfer` to the NEP-141 contract fails, the ERC-20 tokens are permanently destroyed in the EVM with no recovery mechanism. The corresponding NEP-141 balance remains locked inside Aurora's account on the NEP-141 contract, inaccessible to the user. There is no admin function, no re-mint path, and no callback to reverse the burn. The loss is irreversible.

---

### Likelihood Explanation

**Medium.** The failure condition — a recipient NEAR account that has not registered a storage deposit with the NEP-141 contract — is a routine operational state. Any user who attempts to bridge tokens to a freshly created or unregistered NEAR account will silently lose their ERC-20 tokens. No attacker action is required; the victim triggers the loss themselves through a normal, expected user flow. The `error_refund` feature exists precisely to prevent this but is excluded from the production build.

---

### Recommendation

Enable the `error_refund` feature in the production `contract` build profile, or unconditionally populate the `refund` field in `ExitToNearPrecompileCallbackArgs` regardless of the feature flag. The `refund_call_args` helper already exists and correctly constructs the re-mint arguments; it simply needs to be called on every ERC-20 exit path. Alternatively, restructure the code so that a refund callback is always attached for ERC-20 exits, making the refund path the default rather than an opt-in feature.

---

### Proof of Concept

1. Deploy Aurora Engine without the `error_refund` feature (the default production build).
2. Bridge a NEP-141 token to Aurora, receiving ERC-20 tokens at address `erc20`.
3. As a user holding ERC-20 tokens, call `erc20.withdrawToNear(recipient_bytes, amount)` where `recipient` is a valid NEAR account ID that has **not** called `storage_deposit` on the NEP-141 contract.
4. Inside `withdrawToNear`: `_burn(msg.sender, amount)` executes — ERC-20 balance is reduced.
5. The `ExitToNear` precompile fires. Because `error_refund` is not compiled in, `callback_args = { refund: None, transfer_near: None }`.
6. Since `callback_args == default()`, the promise emitted is `PromiseArgs::Create(ft_transfer)` with no callback.
7. The NEAR runtime executes `ft_transfer` on the NEP-141 contract; it fails because the recipient has no storage registration.
8. No callback fires. No re-mint occurs.
9. The user's ERC-20 balance is zero. The NEP-141 tokens remain in Aurora's account. The user has permanently lost their funds. [7](#0-6) [3](#0-2) [8](#0-7)

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

**File:** engine-precompiles/Cargo.toml (L34-39)
```text
[features]
default = ["std"]
std = ["aurora-engine-types/std", "aurora-engine-sdk/bls", "aurora-engine-sdk/std", "aurora-engine-modexp/std", "aurora-evm/std", "ethabi/std", "serde/std", "serde_json/std"]
contract = ["aurora-engine-sdk/contract", "aurora-engine-sdk/bls"]
log = []
error_refund = []
```

**File:** engine/Cargo.toml (L42-50)
```text
[features]
default = ["std"]
std = ["aurora-engine-types/std", "aurora-engine-hashchain/std", "aurora-engine-sdk/std", "aurora-engine-precompiles/std", "aurora-engine-transactions/std", "ethabi/std", "aurora-evm/std", "hex/std", "rlp/std", "serde/std", "serde_json/std"]
contract = ["log", "aurora-engine-sdk/contract", "aurora-engine-precompiles/contract"]
log = ["aurora-engine-sdk/log", "aurora-engine-precompiles/log"]
tracing = ["aurora-evm/tracing"]
error_refund = ["aurora-engine-precompiles/error_refund"]
integration-test = ["log"]
all-promise-actions = ["aurora-engine-sdk/all-promise-actions"]
```

**File:** engine-tests/src/tests/erc20_connector.rs (L656-660)
```rust
        #[cfg(feature = "error_refund")]
        let balance = FT_TRANSFER_AMOUNT.into();
        // If the refund feature is not enabled then there is no refund in the EVM
        #[cfg(not(feature = "error_refund"))]
        let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();
```

**File:** engine/src/contract_methods/connector.rs (L231-241)
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
```
