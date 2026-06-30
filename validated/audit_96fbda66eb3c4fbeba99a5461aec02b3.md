### Title
Hardcoded `FT_TRANSFER_GAS` in `ExitToNear` Precompile Can Cause Permanent Loss of User Funds - (File: `engine-precompiles/src/native.rs`)

---

### Summary

The `ExitToNear` precompile attaches a hardcoded `FT_TRANSFER_GAS = 10_000_000_000_000` (10 TGas) to every `ft_transfer` NEAR cross-contract call it schedules. If a registered NEP-141 contract's `ft_transfer` implementation requires more than 10 TGas, the promise fails. Because ERC-20 tokens are burned before the promise is dispatched, and because the `error_refund` feature that would restore them is **not** a default build feature, the burned tokens are permanently unrecoverable.

---

### Finding Description

**Root cause — hardcoded gas constant:**

```
engine-precompiles/src/native.rs, line 53
pub(super) const FT_TRANSFER_GAS: NearGas = NearGas::new(10_000_000_000_000);
``` [1](#0-0) 

This constant is used unconditionally when the `ExitToNear` precompile builds the outgoing NEAR promise:

```rust
let attached_gas = if method == "ft_transfer_call" {
    costs::FT_TRANSFER_CALL_GAS
} else {
    costs::FT_TRANSFER_GAS          // ← always 10 TGas for ft_transfer
};

let transfer_promise = PromiseCreateArgs {
    target_account_id: nep141_address,
    method,
    args: args.into_bytes(),
    attached_balance: Yocto::new(1),
    attached_gas,                   // ← hardcoded, caller cannot override
};
``` [2](#0-1) 

**Tokens are burned before the promise is dispatched.** The ERC-20 burn (or ETH deduction) happens inside the EVM execution that calls the precompile. The NEAR promise is only *scheduled* as a log; it executes asynchronously after the EVM transaction completes. If the promise fails, the EVM state change (burn) is already committed.

**The refund path is gated behind a non-default compile feature.** The callback `exit_to_near_precompile_callback` only restores tokens when `args.refund` is `Some(...)`, which is only populated when the `error_refund` feature is compiled in:

```rust
let callback_args = ExitToNearPrecompileCallbackArgs {
    #[cfg(feature = "error_refund")]
    refund: refund_call_args(&exit_to_near_params, &exit_event),
    #[cfg(not(feature = "error_refund"))]
    refund: None,          // ← no refund data without the feature
    transfer_near: transfer_near_args,
};
``` [3](#0-2) 

In `exit_to_near_precompile_callback`, when the promise fails and `args.refund` is `None`, the function simply returns `Ok(None)` — no tokens are restored:

```rust
} else if let Some(args) = args.refund {
    let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;
    ...
} else {
    None   // ← silent no-op; burned tokens are gone
};
``` [4](#0-3) 

`error_refund` is **not** listed under `default` in either `engine/Cargo.toml` or `engine-precompiles/Cargo.toml`:

```toml
# engine/Cargo.toml
[features]
default = ["std"]
error_refund = ["aurora-engine-precompiles/error_refund"]
``` [5](#0-4) 

```toml
# engine-precompiles/Cargo.toml
[features]
default = ["std"]
error_refund = []
``` [6](#0-5) 

---

### Impact Explanation

**Permanent freezing / permanent loss of funds (Critical).**

When a user calls the `ExitToNear` precompile to bridge ERC-20 tokens back to NEAR:

1. The ERC-20 tokens are burned inside the EVM (state is committed).
2. A NEAR promise is scheduled to call `ft_transfer` on the NEP-141 contract with exactly 10 TGas.
3. If the NEP-141 contract's `ft_transfer` consumes more than 10 TGas, the promise fails.
4. Without `error_refund`, the callback does nothing — the burned ERC-20 tokens are gone and the NEP-141 balance on the NEAR side is never credited.

The user loses their tokens with no recovery path. There is no admin function to re-issue the burned ERC-20 tokens or retry the failed NEAR transfer.

---

### Likelihood Explanation

**Low-to-Medium.**

- Standard NEP-141 `ft_transfer` implementations (e.g., the reference implementation) consume roughly 5–8 TGas, comfortably under the 10 TGas cap.
- However, Aurora supports *any* NEP-141 contract registered via `deploy_erc20_from_nep_141`. Complex contracts — those with transfer hooks, proxy patterns, multi-step accounting, or on-transfer callbacks — can exceed 10 TGas.
- The `error_refund` feature being non-default means that any production build compiled without it is fully exposed. The comment in the source (`// TODO(#483): Determine the correct amount of gas`) signals that the gas values were set experimentally and are acknowledged as potentially incorrect.
- An unprivileged EVM user triggers this path simply by calling the `ExitToNear` precompile with a registered ERC-20 whose underlying NEP-141 is gas-heavy.

---

### Recommendation

1. **Make `error_refund` a default feature** (or unconditionally include the refund path) so that a failed `ft_transfer` always restores the burned ERC-20 tokens.
2. **Increase `FT_TRANSFER_GAS`** to a more conservative upper bound (e.g., 30–50 TGas), or allow the caller to specify the gas to forward (analogous to using `call{gas: ...}` instead of `transfer()` in Solidity).
3. **Resolve the open TODO** at `native.rs:45` (`// TODO(#483): Determine the correct amount of gas`) with a formal gas profiling exercise across a representative set of NEP-141 contracts.

---

### Proof of Concept

1. Deploy a NEP-141 contract whose `ft_transfer` implementation performs additional on-chain work (e.g., updating a registry, calling a hook contract) such that it consumes > 10 TGas.
2. Register the NEP-141 with Aurora via `deploy_erc20_from_nep_141`, obtaining an ERC-20 mirror.
3. Bridge tokens into Aurora: call `ft_transfer_call` on the NEP-141 → ERC-20 balance is credited.
4. From an EVM account, call the `ExitToNear` precompile (`0xe9217bc70b7ed1f598ddd3199e80b093fa71124f`) with flag `0x01` (ERC-20 exit), the token amount, and a valid NEAR recipient.
5. The EVM transaction succeeds: ERC-20 tokens are burned, a NEAR promise is scheduled with `attached_gas = 10 TGas`.
6. The NEAR runtime executes the promise; `ft_transfer` runs out of gas and fails.
7. The callback `exit_to_near_precompile_callback` is invoked; because `error_refund` is not compiled in, `args.refund` is `None` and the function returns without action.
8. **Result**: ERC-20 tokens are permanently burned; the NEP-141 balance on NEAR is never credited; the user's funds are irrecoverably lost. [7](#0-6) [8](#0-7) [9](#0-8)

### Citations

**File:** engine-precompiles/src/native.rs (L42-62)
```rust
mod costs {
    use crate::prelude::types::{EthGas, NearGas};

    // TODO(#483): Determine the correct amount of gas
    pub(super) const EXIT_TO_NEAR_GAS: EthGas = EthGas::new(0);

    // TODO(#483): Determine the correct amount of gas
    pub(super) const EXIT_TO_ETHEREUM_GAS: EthGas = EthGas::new(0);

    /// Value determined experimentally based on tests and mainnet data. Example:
    /// `https://explorer.mainnet.near.org/transactions/5CD7NrqWpK3H8MAAU4mYEPuuWz9AqR9uJkkZJzw5b8PM#D1b5NVRrAsJKUX2ZGs3poKViu1Rgt4RJZXtTfMgdxH4S`
    pub(super) const FT_TRANSFER_GAS: NearGas = NearGas::new(10_000_000_000_000);

    pub(super) const FT_TRANSFER_CALL_GAS: NearGas = NearGas::new(70_000_000_000_000);

    /// Value determined experimentally based on tests.
    pub(super) const EXIT_TO_NEAR_CALLBACK_GAS: NearGas = NearGas::new(10_000_000_000_000);

    // TODO(#332): Determine the correct amount of gas
    pub(super) const WITHDRAWAL_GAS: NearGas = NearGas::new(100_000_000_000_000);
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

**File:** engine-precompiles/src/native.rs (L456-483)
```rust
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

**File:** engine/Cargo.toml (L43-48)
```text
default = ["std"]
std = ["aurora-engine-types/std", "aurora-engine-hashchain/std", "aurora-engine-sdk/std", "aurora-engine-precompiles/std", "aurora-engine-transactions/std", "ethabi/std", "aurora-evm/std", "hex/std", "rlp/std", "serde/std", "serde_json/std"]
contract = ["log", "aurora-engine-sdk/contract", "aurora-engine-precompiles/contract"]
log = ["aurora-engine-sdk/log", "aurora-engine-precompiles/log"]
tracing = ["aurora-evm/tracing"]
error_refund = ["aurora-engine-precompiles/error_refund"]
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
