### Title
Permanent Fund Freeze When `ft_transfer` Fails in `ExitToNear` Without `error_refund` Feature — (`engine-precompiles/src/native.rs`)

### Summary
When the `error_refund` compile-time feature is absent (it is **not** in the crate's default feature set), a failed `ft_transfer` promise during an `exit_to_near` operation leaves ERC-20 tokens permanently burned in the EVM while the corresponding NEP-141 tokens remain locked inside Aurora's account with no recovery path. This is the direct structural analog of H-10: a fallback path silently absorbs funds into an intermediate holder that lacks any withdrawal mechanism.

### Finding Description
The `ExitToNear` precompile (`engine-precompiles/src/native.rs`) executes a two-step cross-chain exit:

1. **EVM side** – ERC-20 tokens are burned (or ETH is moved to the `exit_to_near` precompile address).
2. **NEAR side** – a `ft_transfer` (or `ft_transfer_call`) promise is scheduled against the NEP-141 contract to deliver tokens to the recipient.

The precompile constructs a `ExitToNearPrecompileCallbackArgs` struct that carries an optional `refund` field:

```rust
// engine-precompiles/src/native.rs  lines 449-455
let callback_args = ExitToNearPrecompileCallbackArgs {
    #[cfg(feature = "error_refund")]
    refund: refund_call_args(&exit_to_near_params, &exit_event),
    #[cfg(not(feature = "error_refund"))]
    refund: None,          // ← always None when feature is absent
    transfer_near: transfer_near_args,
};
``` [1](#0-0) 

When `error_refund` is absent, `refund` is unconditionally `None`. The callback `exit_to_near_precompile_callback` then reaches the failure branch:

```rust
// engine/src/contract_methods/connector.rs  lines 231-241
} else if let Some(args) = args.refund {
    // Exit call failed; need to refund tokens
    let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;
    ...
} else {
    None   // ← silent no-op when refund is None
};
``` [2](#0-1) 

The `error_refund` feature is **not** listed in the crate's `default` features:

```toml
# engine/Cargo.toml  lines 43-48
[features]
default = ["std"]
...
error_refund = ["aurora-engine-precompiles/error_refund"]
``` [3](#0-2) 

Consequently, any production build that does not explicitly pass `--features error_refund` compiles with `refund: None` hardcoded. When `ft_transfer` fails (e.g., recipient account not registered with the NEP-141 contract), the callback executes the `else { None }` branch, performs no state change, and returns successfully — the EVM burn is committed, the NEP-141 transfer never happened, and no re-mint or ETH credit is issued.

The engine's own test suite explicitly documents this loss:

```rust
// engine-tests/src/tests/erc20_connector.rs  lines 771-775
#[cfg(feature = "error_refund")]
let expected_balance = Wei::new_u64(INITIAL_ETH_BALANCE);
// If the refund feature is not enabled, then there is no refund in the EVM
#[cfg(not(feature = "error_refund"))]
let expected_balance = Wei::new_u64(INITIAL_ETH_BALANCE - ETH_EXIT_AMOUNT);
``` [4](#0-3) 

The same pattern applies to the ERC-20 exit path:

```rust
// engine-tests/src/tests/erc20_connector.rs  lines 656-660
#[cfg(feature = "error_refund")]
let balance = FT_TRANSFER_AMOUNT.into();
// If the refund feature is not enabled then there is no refund in the EVM
#[cfg(not(feature = "error_refund"))]
let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();
``` [5](#0-4) 

### Impact Explanation
- **ERC-20 exit path**: ERC-20 tokens are burned in the EVM. If `ft_transfer` fails, the corresponding NEP-141 tokens remain in Aurora's account permanently. The user's ERC-20 balance is reduced; no NEP-141 tokens are delivered; no re-mint occurs. Funds are permanently frozen.
- **ETH exit path**: ETH is moved to the `exit_to_near` precompile address. If `ft_transfer` fails, the ETH is stranded at that address with no recovery mechanism. Funds are permanently frozen.

In both cases the accounting diverges: the EVM state records a burn/transfer that was never matched by a NEAR-side delivery, and there is no on-chain path to recover the stranded value. This matches the **Critical – Permanent freezing of funds** impact class.

### Likelihood Explanation
Any unprivileged EVM user can trigger this by calling `exit_to_near` targeting a NEAR account that is not registered (has no storage deposit) with the NEP-141 contract. This is a routine mistake (e.g., a fresh account, a mistyped account ID, or a contract that does not implement `ft_on_transfer`). The `ft_transfer` call will fail, the callback will silently succeed, and the funds will be lost. No special privilege is required; the entry path is the standard EVM `call` to the exit precompile address.

### Recommendation
1. **Enable `error_refund` in the production build** by adding it to the `contract` feature or the default feature set in `engine/Cargo.toml`, so that `refund_call_args` is always populated and `exit_to_near_precompile_callback` always re-mints/refunds on failure.
2. Alternatively, remove the compile-time gate entirely and make the refund path unconditional, since there is no documented reason to ship without it.
3. Add a guard in `exit_to_near_precompile_callback` that panics or returns an error when `refund` is `None` and the promise failed, so that the transaction reverts rather than silently losing funds.

### Proof of Concept

**ERC-20 exit (no `error_refund` feature):**

1. Alice holds 1000 units of ERC-20 token `T` on Aurora (backed by NEP-141 `T.near`).
2. Alice calls the ERC-20 `withdrawToNear` function targeting `unregistered.near` (an account with no storage deposit in `T.near`).
3. The `ExitToNear` precompile burns 1000 ERC-20 tokens from Alice's EVM balance and schedules `ft_transfer` on `T.near` for `unregistered.near`.
4. `ft_transfer` fails because `unregistered.near` has no storage deposit.
5. `exit_to_near_precompile_callback` is invoked; `args.refund` is `None`; the `else { None }` branch executes — no re-mint, no error.
6. Alice's ERC-20 balance is 0. `T.near` balance of `unregistered.near` is 0. Aurora's `T.near` balance is unchanged (tokens never left). Alice has permanently lost 1000 tokens with no recovery path.

**ETH exit (no `error_refund` feature):**

1. Alice holds ETH on Aurora.
2. Alice calls `withdrawEthToNear` targeting an account whose Aurora NEP-141 balance is 0 (Aurora has no ETH connector tokens to transfer).
3. ETH is moved to `exit_to_near::ADDRESS`; `ft_transfer` on the ETH connector fails.
4. Callback executes the `else { None }` branch — no ETH refund.
5. Alice's ETH is permanently stranded at the precompile address. [6](#0-5) [7](#0-6) [3](#0-2)

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

**File:** engine/src/contract_methods/connector.rs (L214-242)
```rust
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
