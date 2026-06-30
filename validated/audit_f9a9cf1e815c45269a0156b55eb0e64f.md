### Title
Hard-Coded `FT_TRANSFER_GAS` in `ExitToNear` Precompile Causes Permanent Fund Loss When NEP-141 Transfer Fails Without `error_refund` Feature - (File: engine-precompiles/src/native.rs)

---

### Summary

The `ExitToNear` precompile in Aurora Engine attaches a hard-coded NEAR gas budget of **10 TGas** to every `ft_transfer` cross-contract call it schedules. When the `error_refund` Cargo feature is absent from the production build (it is not a default feature), no callback is attached to the promise. If the NEP-141 contract's `ft_transfer` exhausts or exceeds that budget and fails, the ERC-20 tokens that were already burned on the EVM side are never minted on the NEAR side, resulting in **permanent, irrecoverable fund loss** for the user.

---

### Finding Description

**Hard-coded gas constant**

In `engine-precompiles/src/native.rs`, the `costs` module defines:

```rust
pub(super) const FT_TRANSFER_GAS: NearGas = NearGas::new(10_000_000_000_000); // 10 TGas
``` [1](#0-0) 

This constant is unconditionally used as the `attached_gas` for every `ft_transfer` promise created by the `ExitToNear` precompile:

```rust
let attached_gas = if method == "ft_transfer_call" {
    costs::FT_TRANSFER_CALL_GAS
} else {
    costs::FT_TRANSFER_GAS   // ← 10 TGas for ft_transfer
};
let transfer_promise = PromiseCreateArgs {
    target_account_id: nep141_address,
    method,
    args: args.into_bytes(),
    attached_balance: Yocto::new(1),
    attached_gas,
};
``` [2](#0-1) 

**`error_refund` is not a default feature**

`engine-precompiles/Cargo.toml` declares:

```toml
[features]
default = ["std"]
error_refund = []
``` [3](#0-2) 

`engine/Cargo.toml` mirrors this:

```toml
[features]
default = ["std"]
error_refund = ["aurora-engine-precompiles/error_refund"]
``` [4](#0-3) 

The production build task (`build`) passes `${CARGO_FEATURES_BUILD}` which resolves to `contract` — not `contract,error_refund`. [5](#0-4) 

**No callback is attached when `error_refund` is absent**

Without the feature, `callback_args.refund` is forced to `None`:

```rust
let callback_args = ExitToNearPrecompileCallbackArgs {
    #[cfg(feature = "error_refund")]
    refund: refund_call_args(&exit_to_near_params, &exit_event),
    #[cfg(not(feature = "error_refund"))]
    refund: None,
    transfer_near: transfer_near_args,
};
``` [6](#0-5) 

For a standard ETH-base exit or ERC-20 exit (non-wNEAR), `transfer_near_args` is also `None`, making `callback_args` equal to `ExitToNearPrecompileCallbackArgs::default()`. The branch then selects a bare `PromiseArgs::Create` — **no callback at all**:

```rust
let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
    PromiseArgs::Create(transfer_promise)   // ← no error handler
} else {
    PromiseArgs::Callback(PromiseWithCallbackArgs { ... })
};
``` [7](#0-6) 

**Callback logic confirms no refund path**

Even when a callback is attached (e.g., wNEAR unwrap path), `exit_to_near_precompile_callback` only refunds if `args.refund` is `Some(...)`:

```rust
} else if let Some(args) = args.refund {
    // Exit call failed; need to refund tokens
    let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;
    ...
} else {
    None   // ← silent no-op; tokens are gone
};
``` [8](#0-7) 

**Test suite explicitly documents the loss**

```rust
// If the refund feature is not enabled then there is no refund in the EVM
#[cfg(not(feature = "error_refund"))]
let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();
``` [9](#0-8) 

---

### Impact Explanation

**Impact: Critical — Permanent freezing / loss of user funds.**

The ERC-20 burn happens inside the EVM execution phase, before any NEAR promise is dispatched. Once the EVM call succeeds and the burn is committed, the only recovery path is the `ft_transfer` promise succeeding on the NEAR side. If that promise fails (out of gas, unregistered account, or any other reason) and no callback is present, the burned ERC-20 value is irrecoverably destroyed. The user loses the full bridged amount with no on-chain recourse.

---

### Likelihood Explanation

**Likelihood: Medium.**

- Any NEP-141 token whose `ft_transfer` implementation performs additional logic (storage checks, fee-on-transfer hooks, cross-contract sub-calls, or blacklist lookups) can exceed 10 TGas. NEAR's gas pricing has also changed across protocol versions, and the constant carries a `TODO(#483)` marker on the adjacent EVM-gas constant, signalling acknowledged uncertainty.
- The `error_refund` feature is opt-in and absent from the default production build, so the no-refund code path is the live path for every deployed Aurora instance that has not explicitly enabled it.
- The entry point is fully unprivileged: any EVM account can call the `ExitToNear` precompile by invoking the ERC-20 `withdraw` function or sending calldata directly to the precompile address `0xe9217bc70b7ed1f598ddd3199e80b093fa71124f`. [10](#0-9) 

---

### Recommendation

1. **Remove the hard-coded gas cap** or make it a governance-configurable on-chain parameter, mirroring the UniswapX fix of forwarding all available gas.
2. **Enable `error_refund` by default** in the production feature set, or unconditionally attach the callback so that any `ft_transfer` failure triggers a refund regardless of compile-time flags.
3. **Resolve the open TODOs** (`#483`, `#332`) for `EXIT_TO_NEAR_GAS` and `WITHDRAWAL_GAS` before those constants cause analogous issues.

---

### Proof of Concept

1. Deploy a NEP-141 token whose `ft_transfer` function performs a storage-read loop or a sub-call that consumes > 10 TGas.
2. Bridge that token into Aurora (ERC-20 mirror created via `deploy_erc20_token`).
3. From an EVM account, call the ERC-20 `withdraw` function, which burns the ERC-20 balance and invokes the `ExitToNear` precompile.
4. The precompile schedules `ft_transfer` with exactly `10_000_000_000_000` gas and no callback (production build, `error_refund` absent).
5. The NEAR promise fails with `GasExceeded`.
6. Observe: ERC-20 balance is zero; NEP-141 balance of the target NEAR account is unchanged; no refund is issued. Funds are permanently lost. [11](#0-10) [2](#0-1) [7](#0-6) [8](#0-7)

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

**File:** engine-precompiles/src/native.rs (L270-278)
```rust
pub mod exit_to_near {
    use crate::prelude::types::{Address, make_address};

    /// Exit to NEAR precompile address
    ///
    /// Address: `0xe9217bc70b7ed1f598ddd3199e80b093fa71124f`
    /// This address is computed as: `&keccak("exitToNear")[12..]`
    pub const ADDRESS: Address = make_address(0xe9217bc7, 0x0b7ed1f598ddd3199e80b093fa71124f);
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

**File:** engine-precompiles/src/native.rs (L456-468)
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

**File:** engine-precompiles/Cargo.toml (L34-39)
```text
[features]
default = ["std"]
std = ["aurora-engine-types/std", "aurora-engine-sdk/bls", "aurora-engine-sdk/std", "aurora-engine-modexp/std", "aurora-evm/std", "ethabi/std", "serde/std", "serde_json/std"]
contract = ["aurora-engine-sdk/contract", "aurora-engine-sdk/bls"]
log = []
error_refund = []
```

**File:** engine/Cargo.toml (L42-48)
```text
[features]
default = ["std"]
std = ["aurora-engine-types/std", "aurora-engine-hashchain/std", "aurora-engine-sdk/std", "aurora-engine-precompiles/std", "aurora-engine-transactions/std", "ethabi/std", "aurora-evm/std", "hex/std", "rlp/std", "serde/std", "serde_json/std"]
contract = ["log", "aurora-engine-sdk/contract", "aurora-engine-precompiles/contract"]
log = ["aurora-engine-sdk/log", "aurora-engine-precompiles/log"]
tracing = ["aurora-evm/tracing"]
error_refund = ["aurora-engine-precompiles/error_refund"]
```

**File:** Makefile.toml (L307-310)
```text
[tasks.build]
env = { "RUSTFLAGS" = "-C strip=symbols --remap-path-prefix ${HOME}=/path/to/home/ --remap-path-prefix ${PWD}=/path/to/source/", "CARGO_FEATURES" = "${CARGO_FEATURES_BUILD}", "RELEASE" = "--release", "TARGET_DIR" = "release" }
category = "Build"
run_task = "build-engine-flow"
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
