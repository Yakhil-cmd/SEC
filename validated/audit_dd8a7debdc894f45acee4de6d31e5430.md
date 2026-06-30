### Title
Hardcoded `FT_TRANSFER_GAS` in `ExitToNear` Precompile Can Permanently Freeze Bridged Funds - (File: engine-precompiles/src/native.rs)

### Summary

The `ExitToNear` precompile attaches a hardcoded `FT_TRANSFER_GAS = 10 TGas` to every `ft_transfer` NEAR promise it schedules when bridging ERC-20 tokens out of Aurora. If the target NEP-141 contract's `ft_transfer` consumes more than 10 TGas (due to complex logic, storage operations, or proxy indirection), the promise fails with out-of-gas. When the `error_refund` compile-time feature is absent, no refund path exists: the EVM tokens have already been burned, and the NEAR-side transfer never completes, permanently freezing the user's funds.

### Finding Description

When an EVM user exits ERC-20 tokens to NEAR via the `ExitToNear` precompile, the following sequence occurs:

1. The ERC-20 contract burns the user's tokens (EVM state is mutated).
2. The precompile emits a promise log encoding a `PromiseCreateArgs` targeting the mapped NEP-141 contract's `ft_transfer` method.
3. The NEAR runtime executes that promise with exactly `FT_TRANSFER_GAS = 10_000_000_000_000` gas (10 TGas).

The gas constant is hardcoded:

```rust
// engine-precompiles/src/native.rs, line 53
pub(super) const FT_TRANSFER_GAS: NearGas = NearGas::new(10_000_000_000_000);
``` [1](#0-0) 

It is selected and attached at promise-construction time:

```rust
let attached_gas = if method == "ft_transfer_call" {
    costs::FT_TRANSFER_CALL_GAS
} else {
    costs::FT_TRANSFER_GAS   // ← 10 TGas, hardcoded
};

let transfer_promise = PromiseCreateArgs {
    target_account_id: nep141_address,
    method,
    args: args.into_bytes(),
    attached_balance: Yocto::new(1),
    attached_gas,              // ← fixed ceiling
};
``` [2](#0-1) 

When the `error_refund` feature is **not** compiled in, the callback's `refund` field is `None`:

```rust
#[cfg(not(feature = "error_refund"))]
refund: None,
``` [3](#0-2) 

In `exit_to_near_precompile_callback`, a failed promise with `refund: None` falls through to the `else { None }` branch — no refund is issued:

```rust
} else if let Some(args) = args.refund {
    // Exit call failed; need to refund tokens
    let refund_result = engine::refund_on_error(...)?;
    ...
} else {
    None   // ← silent no-op; tokens are gone
};
``` [4](#0-3) 

Even when `error_refund` **is** compiled in, the callback itself is allocated only `EXIT_TO_NEAR_CALLBACK_GAS = 10 TGas`:

```rust
pub(super) const EXIT_TO_NEAR_CALLBACK_GAS: NearGas = NearGas::new(10_000_000_000_000);
``` [5](#0-4) 

If the callback's own execution (which calls `engine::refund_on_error`) exceeds that budget, the refund also fails, and `ERR_REFUND_FAILURE` is returned — but the EVM tokens remain burned. [6](#0-5) 

### Impact Explanation

**Critical — Permanent freezing of funds.**

ERC-20 tokens are burned from the user's Aurora EVM balance before the NEAR promise is dispatched. If the `ft_transfer` promise fails due to out-of-gas and no refund path is active, the tokens are destroyed on the EVM side and never credited on the NEAR side. The user loses the full bridged amount with no recovery mechanism.

### Likelihood Explanation

**Medium.** The NEP-141 contract targeted by `ExitToNear` is determined by the ERC-20-to-NEP-141 mapping stored in Aurora's state. Any NEP-141 contract whose `ft_transfer` implementation exceeds 10 TGas — due to complex accounting logic, storage-staking checks, cross-contract reads, or proxy indirection — triggers this condition. NEAR's gas model makes 10 TGas a relatively tight budget for non-trivial fungible token contracts. The condition is reachable by any unprivileged EVM user who holds a bridged ERC-20 token and calls its `withdraw` function.

### Recommendation

1. **Replace the hardcoded `FT_TRANSFER_GAS` with a dynamic value** derived from the remaining prepaid gas at promise-creation time (analogous to how `calculate_attached_gas` works in `engine/src/contract_methods/connector.rs`), so the promise receives all gas the transaction has left rather than a fixed ceiling. [7](#0-6) 

2. **Unconditionally enable the `error_refund` refund path** (or make it the default) so that any `ft_transfer` failure always triggers a re-mint of the burned ERC-20 tokens on the EVM side.

3. **Increase `EXIT_TO_NEAR_CALLBACK_GAS`** to a value sufficient to cover `refund_on_error` execution, or likewise make it dynamic.

4. Resolve the open `TODO(#332)` and `TODO(#483)` items that acknowledge the gas values are not yet correctly determined. [8](#0-7) 

### Proof of Concept

1. Deploy a NEP-141 contract whose `ft_transfer` method performs enough storage operations to consume > 10 TGas.
2. Bridge that NEP-141 as an ERC-20 on Aurora via `deploy_erc20_token`.
3. Acquire the ERC-20 tokens on Aurora.
4. Call the ERC-20's `withdraw` function (which invokes the `ExitToNear` precompile with flag `0x01`).
5. The EVM burns the tokens; a NEAR promise is scheduled with `attached_gas = 10 TGas`.
6. The NEAR runtime executes `ft_transfer` on the NEP-141 contract; it runs out of gas and the promise fails.
7. Without `error_refund`: the callback receives `refund: None`, takes the `else { None }` branch, and returns `Ok(None)` — no refund.
8. Observe: ERC-20 balance on Aurora is zero; NEP-141 balance on NEAR is unchanged. Funds are permanently lost. [9](#0-8)

### Citations

**File:** engine-precompiles/src/native.rs (L45-61)
```rust
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
```

**File:** engine-precompiles/src/native.rs (L452-453)
```rust
            #[cfg(not(feature = "error_refund"))]
            refund: None,
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

**File:** engine/src/contract_methods/connector.rs (L587-596)
```rust
// TODO: Return `Result` with an error about lacking of gas instead.
fn calculate_attached_gas<E: Env>(env: &E) -> NearGas {
    let required_gas = env.used_gas().saturating_add(GAS_FOR_PROMISE_CREATION);

    if required_gas >= env.prepaid_gas() {
        NearGas::new(0)
    } else {
        env.prepaid_gas() - required_gas
    }
}
```
