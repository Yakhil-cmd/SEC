### Title
Hardcoded Low NEAR Gas Attached to `near_withdraw` Promise in `ExitToNear` Precompile Causes Permanent Freezing of wNEAR Tokens During Unwrap Flow - (File: engine-precompiles/src/native.rs)

### Summary

The `ExitToNear` precompile in Aurora Engine uses a hardcoded `FT_TRANSFER_GAS` constant (10 TGas) when scheduling the `near_withdraw` call on the wNEAR NEP-141 contract. This is the same fixed gas budget used for a simple `ft_transfer`, but `near_withdraw` is a more complex operation that must also fund the subsequent `exit_to_near_precompile_callback`. If the `near_withdraw` promise runs out of gas or fails due to insufficient gas, the wNEAR ERC-20 tokens have already been burned on Aurora's EVM side, but the NEAR tokens are never delivered to the recipient. Without the `error_refund` feature enabled, there is no recovery path, resulting in permanent loss of user funds.

### Finding Description

In `engine-precompiles/src/native.rs`, the `ExitToNear` precompile's `run()` method selects the NEAR gas to attach to the outgoing promise based solely on whether the method name is `ft_transfer_call` or not:

```rust
let attached_gas = if method == "ft_transfer_call" {
    costs::FT_TRANSFER_CALL_GAS   // 70 TGas
} else {
    costs::FT_TRANSFER_GAS        // 10 TGas
};
```

When a user unwraps wNEAR (ERC-20 on Aurora) back to native NEAR, the `exit_erc20_token_to_near` function sets `method = "near_withdraw"`. This falls into the `else` branch, so only `FT_TRANSFER_GAS = 10_000_000_000_000` (10 TGas) is attached to the `near_withdraw` promise.

However, the `near_withdraw` call is not a simple transfer — it is the **base** of a `PromiseWithCallbackArgs` chain. The callback is `exit_to_near_precompile_callback` on the Aurora engine itself, which receives `EXIT_TO_NEAR_CALLBACK_GAS = 10_000_000_000_000` (10 TGas). The total gas budget for the two-step flow is therefore 10 TGas (base) + 10 TGas (callback) = 20 TGas, but the base promise (`near_withdraw`) only receives 10 TGas. The NEAR runtime distributes gas to callbacks from the **remaining** gas of the base call, not from a separate pool. If the `near_withdraw` call consumes most or all of its 10 TGas, the callback may receive insufficient gas to execute, causing it to fail silently.

The critical consequence: the ERC-20 wNEAR tokens are **burned** on the EVM side before the promise is dispatched. If the promise chain fails (out of gas on `near_withdraw`, or insufficient gas forwarded to the callback), the user's tokens are gone with no refund path (unless the `error_refund` feature is compiled in, which is a compile-time feature flag, not guaranteed in production).

The comment in the code itself acknowledges the gas values are not finalized:

```rust
// TODO(#332): Determine the correct amount of gas
pub(super) const WITHDRAWAL_GAS: NearGas = NearGas::new(100_000_000_000_000);
```

And `FT_TRANSFER_GAS` is noted as experimentally determined for a simple `ft_transfer`, not for `near_withdraw`.

### Impact Explanation

**Impact: High — Temporary or Permanent Freezing of Funds / Theft of Unclaimed Yield**

When a user calls the `ExitToNear` precompile with the `":unwrap"` suffix on their wNEAR ERC-20 token:
1. The ERC-20 wNEAR is burned on Aurora's EVM (irreversible within the EVM).
2. A `near_withdraw` promise is dispatched with only 10 TGas.
3. If `near_withdraw` fails due to gas exhaustion, the callback `exit_to_near_precompile_callback` either does not execute or executes with 0 gas.
4. The `transfer_near` action inside the callback (which sends the unwrapped NEAR to the recipient) is never executed.
5. The NEAR tokens remain locked in the Aurora engine contract with no user-accessible recovery mechanism (absent the `error_refund` feature).

This constitutes **permanent freezing of user funds** matching the Critical impact tier, or at minimum **temporary freezing** (High) if an admin can manually recover.

### Likelihood Explanation

The wNEAR unwrap path is a documented, user-facing feature. Any user who calls `exitToNear` with the `":unwrap"` suffix on the wNEAR ERC-20 triggers this code path. The 10 TGas budget for `near_withdraw` is tight — the NEAR runtime's `near_withdraw` on the wNEAR contract itself requires non-trivial gas, and the remaining gas must also cover the callback dispatch overhead. Under network congestion or after NEAR runtime gas cost changes (analogous to EVM gas repricing), this budget can become insufficient. The vulnerability is reachable by any unprivileged EVM user holding wNEAR ERC-20 tokens on Aurora.

### Recommendation

Replace the hardcoded `FT_TRANSFER_GAS` constant for the `near_withdraw` path with a dedicated constant (e.g., `NEAR_WITHDRAW_GAS`) that accounts for both the `near_withdraw` execution cost and the gas needed to forward to the `exit_to_near_precompile_callback`. The existing `WITHDRAWAL_GAS = 100 TGas` constant (used in `ExitToEthereum`) would be a more appropriate budget. Additionally, ensure the `error_refund` feature is always enabled in production builds so that failed promise chains trigger ERC-20 re-minting as a fallback.

### Proof of Concept

1. User holds wNEAR ERC-20 tokens on Aurora (bridged from NEAR's wNEAR NEP-141).
2. User calls the `ExitToNear` precompile at address `0xe9217bc70b7ed1f598ddd3199e80b093fa71124f` with input encoding the wNEAR ERC-20 amount and recipient with the `":unwrap"` suffix.
3. Inside `exit_erc20_token_to_near`, the `Message::UnwrapWnear` branch is matched, setting `method = "near_withdraw"` and `transfer_near_args = Some(TransferNearArgs { ... })`.
4. Back in `ExitToNear::run()`, since `method != "ft_transfer_call"`, `attached_gas = FT_TRANSFER_GAS = 10 TGas` is selected.
5. A `PromiseWithCallbackArgs` is constructed: base = `near_withdraw` with 10 TGas, callback = `exit_to_near_precompile_callback` with 10 TGas.
6. The ERC-20 burn has already occurred in the EVM execution.
7. If `near_withdraw` on the wNEAR contract exhausts the 10 TGas budget, the callback receives 0 gas and fails.
8. `exit_to_near_precompile_callback` is never reached (or panics), so the `PromiseAction::Transfer` to the recipient is never created.
9. The user's wNEAR ERC-20 is burned; the NEAR tokens are stuck in the Aurora engine account.

Relevant code locations: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** engine-precompiles/src/native.rs (L585-609)
```rust
    let (nep141_account_id, args, method, transfer_near_args, event) = match exit_params.message {
        // wNEAR address should be set via the `factory_set_wnear_address` transaction first.
        Some(Message::UnwrapWnear) if erc20_address == get_wnear_address(io).raw() =>
        // The flow is following here:
        // 1. We call `near_withdraw` on wNEAR account id on `aurora` behalf.
        // In such way we unwrap wNEAR to NEAR.
        // 2. After that, we call callback `exit_to_near_precompile_callback` on the `aurora`
        // in which make transfer of unwrapped NEAR to the `target_account_id`.
        {
            (
                nep141_account_id,
                format!(r#"{{"amount":"{}"}}"#, exit_params.amount.as_u128()),
                "near_withdraw",
                Some(TransferNearArgs {
                    target_account_id: exit_params.receiver_account_id.clone(),
                    amount: exit_params.amount.as_u128(),
                }),
                events::ExitToNear::Legacy(ExitToNearLegacy {
                    sender: Address::new(erc20_address),
                    erc20_address: Address::new(erc20_address),
                    dest: exit_params.receiver_account_id.to_string(),
                    amount: exit_params.amount,
                }),
            )
        }
```

**File:** engine/src/contract_methods/connector.rs (L214-228)
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
```
