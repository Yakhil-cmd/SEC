### Title
Hardcoded NEAR Gas Stipend for Bridge Exit Calls May Become Insufficient, Permanently Freezing User Funds - (File: engine-precompiles/src/native.rs)

### Summary
The `ExitToNear` and `ExitToEthereum` precompiles use hardcoded NEAR gas amounts for the NEAR-side promise calls they create. If NEAR protocol increases the gas cost of storage or fungible-token operations (analogous to EIP-1884 raising SLOAD costs), these fixed stipends will be insufficient, causing the exit promise to fail. For regular ERC-20 exits without the `error_refund` feature enabled, no refund callback is attached, so the tokens burned on the EVM side are permanently unrecoverable.

### Finding Description
In `engine-precompiles/src/native.rs`, the `costs` module defines hardcoded NEAR gas values used when constructing NEAR promises from the bridge exit precompiles:

```rust
// TODO(#483): Determine the correct amount of gas
pub(super) const EXIT_TO_NEAR_GAS: EthGas = EthGas::new(0);

// TODO(#483): Determine the correct amount of gas
pub(super) const EXIT_TO_ETHEREUM_GAS: EthGas = EthGas::new(0);

pub(super) const FT_TRANSFER_GAS: NearGas = NearGas::new(10_000_000_000_000);   // 10 TGas
pub(super) const FT_TRANSFER_CALL_GAS: NearGas = NearGas::new(70_000_000_000_000); // 70 TGas
pub(super) const EXIT_TO_NEAR_CALLBACK_GAS: NearGas = NearGas::new(10_000_000_000_000); // 10 TGas
// TODO(#332): Determine the correct amount of gas
pub(super) const WITHDRAWAL_GAS: NearGas = NearGas::new(100_000_000_000_000);   // 100 TGas
``` [1](#0-0) 

The `FT_TRANSFER_GAS` constant is used directly as `attached_gas` when constructing the `ft_transfer` promise in the `ExitToNear::run` path:

```rust
let attached_gas = if method == "ft_transfer_call" {
    costs::FT_TRANSFER_CALL_GAS
} else {
    costs::FT_TRANSFER_GAS
};
let transfer_promise = PromiseCreateArgs {
    ...
    attached_gas,
};
``` [2](#0-1) 

For a regular ERC-20 exit (non-wNEAR, no `ft_transfer_call`), the callback is only attached when `callback_args != default()`. Without the `error_refund` compile-time feature, `refund` is `None`, and for non-wNEAR exits `transfer_near` is also `None`, so `callback_args == default()` and **no callback is attached**:

```rust
let callback_args = ExitToNearPrecompileCallbackArgs {
    #[cfg(feature = "error_refund")]
    refund: refund_call_args(&exit_to_near_params, &exit_event),
    #[cfg(not(feature = "error_refund"))]
    refund: None,
    transfer_near: transfer_near_args,
};
let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
    PromiseArgs::Create(transfer_promise)   // no refund callback
} else {
    PromiseArgs::Callback(...)
};
``` [3](#0-2) 

Similarly, `WITHDRAWAL_GAS = 100 TGas` is hardcoded for the `ExitToEthereum` `withdraw` promise: [4](#0-3) 

The same pattern exists in the XCC subsystem with `WITHDRAW_GAS = 40 TGas` and `REFUND_GAS = 5 TGas`: [5](#0-4) 

### Impact Explanation
When a user calls `withdrawToNear` on an ERC-20 contract on Aurora, the ERC-20 tokens are burned on the EVM side first. The `ExitToNear` precompile then schedules a NEAR `ft_transfer` promise with exactly `FT_TRANSFER_GAS = 10 TGas`. If NEAR protocol raises the gas cost of storage reads or NEP-141 operations (as EIP-1884 raised SLOAD costs), the `ft_transfer` call will run out of gas and fail. Because no refund callback is attached (without `error_refund` feature), the burned ERC-20 tokens are permanently unrecoverable — a **critical permanent fund freeze**.

The two TODO comments (`#483`, `#332`) in the same `costs` module confirm the developers have not yet determined correct gas values, indicating this is an acknowledged open risk.

### Likelihood Explanation
NEAR protocol has a history of adjusting gas costs as the network evolves. The hardcoded values were determined experimentally at a point in time. Any future NEAR protocol upgrade that increases the gas cost of `ft_transfer`, storage access, or cross-contract dispatch could make `10 TGas` insufficient. The risk is directly analogous to EIP-1884 making Solidity's 2,300-gas `.transfer()` stipend insufficient: the root cause is the hardcoded constant in the Aurora Engine source, not the protocol change itself.

### Recommendation
1. Replace hardcoded NEAR gas constants with configurable, on-chain-updatable parameters (e.g., stored in engine state and settable by the owner), so they can be adjusted without a contract upgrade when NEAR protocol gas costs change.
2. Always attach a refund/error-recovery callback to exit promises (enable `error_refund` unconditionally, or implement an equivalent recovery path), so that if the NEAR-side call fails, the burned EVM tokens are re-minted to the user.
3. Resolve the two open TODOs (`#483`, `#332`) for `EXIT_TO_NEAR_GAS` and `WITHDRAWAL_GAS` before production use.

### Proof of Concept
1. User holds ERC-20 tokens on Aurora and calls `withdrawToNear(recipient, amount)`.
2. The ERC-20 contract burns `amount` tokens and calls the `ExitToNear` precompile.
3. The precompile constructs a `PromiseCreateArgs` with `attached_gas = FT_TRANSFER_GAS = 10_000_000_000_000` (10 TGas) and no callback (since `error_refund` is not enabled and this is not a wNEAR exit).
4. NEAR protocol executes the `ft_transfer` call; if gas costs have increased and 10 TGas is insufficient, the promise fails.
5. No callback fires; the ERC-20 tokens remain burned on the EVM side with no recovery path.
6. User's funds are permanently frozen.

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

**File:** engine-precompiles/src/native.rs (L977-983)
```rust
        let withdraw_promise = PromiseCreateArgs {
            target_account_id: nep141_address,
            method: "withdraw".to_string(),
            args: serialized_args,
            attached_balance: Yocto::new(1),
            attached_gas: costs::WITHDRAWAL_GAS,
        };
```

**File:** engine/src/xcc.rs (L24-29)
```rust
/// Gas costs estimated from simulation tests.
pub const VERSION_UPDATE_GAS: NearGas = NearGas::new(5_000_000_000_000);
pub const INITIALIZE_GAS: NearGas = NearGas::new(15_000_000_000_000);
pub const UPGRADE_GAS: NearGas = NearGas::new(20_000_000_000_000);
pub const REFUND_GAS: NearGas = NearGas::new(5_000_000_000_000);
pub const WITHDRAW_GAS: NearGas = NearGas::new(40_000_000_000_000);
```
