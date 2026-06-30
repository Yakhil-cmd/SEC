### Title
Permanent Loss of ERC-20/ETH Tokens When `ExitToNear` Bridge Promise Fails Without `error_refund` Feature — (`engine-precompiles/src/native.rs`, `engine/src/contract_methods/connector.rs`)

---

### Summary

When a user bridges ERC-20 tokens (or ETH) from Aurora to NEAR via the `ExitToNear` precompile and the downstream NEAR `ft_transfer` promise fails, the user's tokens are permanently lost if the `error_refund` compile-time feature is not enabled. The ERC-20 tokens are burned on Aurora but never re-minted, and the NEP-141 tokens remain locked in Aurora's NEAR account with no recovery path.

---

### Finding Description

The `ExitToNear` precompile in `engine-precompiles/src/native.rs` handles bridging of ERC-20 tokens and ETH from Aurora (EVM) to NEAR. The flow is:

1. User calls the precompile; ERC-20 tokens are burned (or ETH is transferred to the precompile address).
2. A NEAR promise (`ft_transfer` or `ft_transfer_call`) is scheduled to transfer the corresponding NEP-141 tokens to the recipient.
3. A callback (`exit_to_near_precompile_callback`) is optionally registered to handle promise failure and re-mint the burned tokens.

The critical issue is in how the callback's `refund` field is populated:

```rust
// engine-precompiles/src/native.rs:449-454
let callback_args = ExitToNearPrecompileCallbackArgs {
    #[cfg(feature = "error_refund")]
    refund: refund_call_args(&exit_to_near_params, &exit_event),
    #[cfg(not(feature = "error_refund"))]
    refund: None,   // <-- always None when feature is disabled
    transfer_near: transfer_near_args,
};
```

When `error_refund` is not enabled, `refund` is hardcoded to `None`. In the callback:

```rust
// engine/src/contract_methods/connector.rs:231-242
} else if let Some(args) = args.refund {
    // Exit call failed; need to refund tokens
    let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;
    ...
} else {
    None  // <-- taken when refund is None; no recovery action
};
```

When the NEAR promise fails and `args.refund` is `None`, the `else { None }` branch is taken silently. No re-minting of ERC-20 tokens occurs, and no ETH is returned. The user's assets are permanently destroyed.

Furthermore, for the common case of a plain ERC-20 `ft_transfer` exit (no wNEAR unwrap), `transfer_near` is also `None`, making `callback_args == ExitToNearPrecompileCallbackArgs::default()`. This causes the promise to be scheduled as `PromiseArgs::Create` (no callback at all), meaning there is no opportunity to detect or handle failure:

```rust
// engine-precompiles/src/native.rs:470-483
let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
    PromiseArgs::Create(transfer_promise)  // no callback registered
} else {
    PromiseArgs::Callback(...)
};
```

The `error_refund` feature is not part of the `default` feature set in either `engine/Cargo.toml` or `engine-precompiles/Cargo.toml`, meaning any deployment built without explicitly enabling it is vulnerable.

The test suite itself acknowledges this behavior:

```rust
// engine-tests/src/tests/erc20_connector.rs:658-660
// If the refund feature is not enabled then there is no refund in the EVM
#[cfg(not(feature = "error_refund"))]
let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();
```

---

### Impact Explanation

**Critical — Permanent loss of user funds.**

When `error_refund` is not compiled in:
- A user's ERC-20 tokens are burned on Aurora (removed from their balance permanently).
- The NEAR-side `ft_transfer` fails for any reason (recipient account not registered with the NEP-141 contract, insufficient Aurora NEP-141 balance, network issues, etc.).
- No re-minting occurs; the ERC-20 tokens are gone.
- The NEP-141 tokens remain locked in Aurora's NEAR account, inaccessible to the user.

The user suffers a complete, unrecoverable loss of the bridged token amount. This is not a gas/fee loss but a loss of the principal token value itself.

---

### Likelihood Explanation

**Medium.** The `ft_transfer` promise can fail for multiple realistic reasons reachable by ordinary users:
- Sending to a NEAR account not registered with the NEP-141 contract (storage deposit not paid).
- Aurora's NEP-141 balance being insufficient (e.g., drained by other operations).
- Any NEAR-side panic in the NEP-141 contract.

Any EVM user who calls the `ExitToNear` precompile (directly or via an ERC-20 contract's exit function) is exposed. The likelihood depends on whether `error_refund` is enabled in the production build; if it is not, every failed exit is a permanent loss.

---

### Recommendation

1. Enable the `error_refund` feature in all production builds by adding it to the `default` feature set, or make it unconditional (remove the feature flag and always populate `refund`).
2. For the plain `ft_transfer` case (where no callback is currently registered when `error_refund` is off), always register a callback so that promise failures can be detected and handled.
3. If the feature flag must remain, document clearly that deployments without `error_refund` will permanently lose user tokens on exit failure.

---

### Proof of Concept

**Root cause — `refund` is always `None` without the feature:** [1](#0-0) 

**No callback registered for plain `ft_transfer` exits when `callback_args == default()`:** [2](#0-1) 

**Callback silently does nothing when `refund` is `None` and promise failed:** [3](#0-2) 

**`error_refund` is not in the `default` feature set:** [4](#0-3) [5](#0-4) 

**Test explicitly confirms token loss when feature is disabled:** [6](#0-5) [7](#0-6)

### Citations

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

**File:** engine-tests/src/tests/erc20_connector.rs (L656-665)
```rust
        #[cfg(feature = "error_refund")]
        let balance = FT_TRANSFER_AMOUNT.into();
        // If the refund feature is not enabled then there is no refund in the EVM
        #[cfg(not(feature = "error_refund"))]
        let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();

        assert_eq!(
            erc20_balance(&erc20, ft_owner_address, &aurora).await,
            balance
        );
```

**File:** engine-tests/src/tests/erc20_connector.rs (L771-780)
```rust
        #[cfg(feature = "error_refund")]
        let expected_balance = Wei::new_u64(INITIAL_ETH_BALANCE);
        // If the refund feature is not enabled, then there is no refund in the EVM
        #[cfg(not(feature = "error_refund"))]
        let expected_balance = Wei::new_u64(INITIAL_ETH_BALANCE - ETH_EXIT_AMOUNT);

        assert_eq!(
            eth_balance_of(signer_address, &aurora).await,
            expected_balance
        );
```
