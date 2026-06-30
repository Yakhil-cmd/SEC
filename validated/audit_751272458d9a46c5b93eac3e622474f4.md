### Title
Missing ERC-20 Re-mint on Failed `ft_transfer` When `error_refund` Feature Is Disabled - (`engine-precompiles/src/native.rs`, `etc/eth-contracts/contracts/EvmErc20.sol`)

---

### Summary

When the Aurora Engine is compiled without the `error_refund` feature (which is **not** in the default feature set), a user calling `withdrawToNear` on an `EvmErc20` contract targeting an unregistered NEP-141 recipient will have their ERC-20 tokens permanently burned with no re-mint on failure. The NEP-141 tokens remain locked in Aurora's connector account with no user-accessible recovery path.

---

### Finding Description

The flow has three distinct stages, each with a concrete code reference:

**Stage 1 — ERC-20 burn (unconditional)**

`EvmErc20.withdrawToNear` burns the caller's tokens *before* the cross-contract call is even scheduled:

```solidity
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);   // ← tokens destroyed here
    ...
    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, ...)
    }
}
``` [1](#0-0) 

**Stage 2 — Callback args construction without `error_refund`**

In the `ExitToNear` precompile, when the feature is absent, `refund` is hardcoded to `None`. For a plain `ft_transfer` (non-wNEAR) exit, `transfer_near_args` is also `None`. This makes `callback_args` equal to `ExitToNearPrecompileCallbackArgs::default()`:

```rust
let callback_args = ExitToNearPrecompileCallbackArgs {
    #[cfg(feature = "error_refund")]
    refund: refund_call_args(&exit_to_near_params, &exit_event),
    #[cfg(not(feature = "error_refund"))]
    refund: None,          // ← always None without the feature
    transfer_near: transfer_near_args,  // ← None for plain ft_transfer
};
``` [2](#0-1) 

**Stage 3 — No callback is scheduled at all**

Because `callback_args == Default::default()`, the promise degrades to a bare `PromiseArgs::Create` with no attached callback:

```rust
let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
    PromiseArgs::Create(transfer_promise)   // ← no callback
} else {
    PromiseArgs::Callback(PromiseWithCallbackArgs { ... })
};
``` [3](#0-2) 

`exit_to_near_precompile_callback` is therefore **never invoked**. When `ft_transfer` fails on the NEAR side (e.g., recipient has no storage deposit), the failure is silently swallowed. The refund branch in the callback that would call `engine::refund_on_error` and re-mint the ERC-20 tokens is unreachable:

```rust
} else if let Some(args) = args.refund {
    // Exit call failed; need to refund tokens
    let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;
    ...
``` [4](#0-3) 

**Feature is not in defaults**

`error_refund` is an opt-in feature absent from both `engine/Cargo.toml` and `engine-precompiles/Cargo.toml` default feature sets:

```toml
[features]
default = ["std"]
...
error_refund = ["aurora-engine-precompiles/error_refund"]
``` [5](#0-4) 

```toml
[features]
default = ["std"]
...
error_refund = []
``` [6](#0-5) 

---

### Impact Explanation

- User's ERC-20 balance is reduced to zero (tokens burned).
- The NEP-141 tokens remain in Aurora's connector account (`aurora.id()`), inaccessible to the user.
- There is no on-chain recovery path: no re-mint, no refund promise, no admin escape hatch for the individual user.
- Impact: **High — Theft of unclaimed yield** (user permanently loses bridged token value).

---

### Likelihood Explanation

- Any user can trigger this by calling `withdrawToNear` with a recipient that has not registered storage with the NEP-141 contract. No special privilege is required.
- The attacker does not need to be the victim; a victim can self-trigger this accidentally.
- The condition (unregistered recipient) is common and easy to reach.
- The existing test suite explicitly documents and confirms this loss:

```rust
// If the refund feature is not enabled then there is no refund in the EVM
#[cfg(not(feature = "error_refund"))]
let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();
``` [7](#0-6) 

---

### Recommendation

Enable `error_refund` as a **default feature** in both `engine/Cargo.toml` and `engine-precompiles/Cargo.toml`, or remove the feature gate entirely and always populate `refund` with the re-mint args. The `refund_call_args` function already exists and is correct; the only change needed is removing the `#[cfg(not(feature = "error_refund"))] refund: None` arm so the refund args are always serialized into the callback. [8](#0-7) 

---

### Proof of Concept

The existing test `test_exit_to_near_refund` in `engine-tests/src/tests/erc20_connector.rs` is a complete, locally runnable proof of concept on unmodified code:

1. Deploy Aurora engine **without** `--features error_refund`.
2. Deploy a NEP-141 token; bridge it to an ERC-20 on Aurora.
3. Call `exit_to_near` targeting `"unregistered.near"` (no storage deposit on the NEP-141).
4. The `ft_transfer` promise fails on the NEAR side.
5. Assert: `erc20_balance == FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT` (tokens gone, no re-mint).
6. Assert: NEP-141 balance of `aurora.id()` is unchanged (tokens stuck in connector). [9](#0-8)

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

**File:** engine-precompiles/src/native.rs (L699-725)
```rust
#[cfg(feature = "error_refund")]
#[allow(clippy::unnecessary_wraps)]
fn refund_call_args(
    params: &ExitToNearParams,
    event: &events::ExitToNear,
) -> Option<RefundCallArgs> {
    Some(RefundCallArgs {
        recipient_address: match params {
            ExitToNearParams::BaseToken(params) => params.refund_address,
            ExitToNearParams::Erc20TokenParams(params) => params.refund_address,
        },
        erc20_address: match params {
            ExitToNearParams::BaseToken(_) => None,
            ExitToNearParams::Erc20TokenParams(_) => {
                let erc20_address = match event {
                    events::ExitToNear::Legacy(legacy) => legacy.erc20_address,
                    events::ExitToNear::Omni(omni) => omni.erc20_address,
                };
                Some(erc20_address)
            }
        },
        amount: types::u256_to_arr(&match event {
            events::ExitToNear::Legacy(legacy) => legacy.amount,
            events::ExitToNear::Omni(omni) => omni.amount,
        }),
    })
}
```

**File:** engine/src/contract_methods/connector.rs (L231-239)
```rust
        } else if let Some(args) = args.refund {
            // Exit call failed; need to refund tokens
            let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;

            if !refund_result.status.is_ok() {
                return Err(errors::ERR_REFUND_FAILURE.into());
            }

            Some(refund_result)
```

**File:** engine/Cargo.toml (L42-49)
```text
[features]
default = ["std"]
std = ["aurora-engine-types/std", "aurora-engine-hashchain/std", "aurora-engine-sdk/std", "aurora-engine-precompiles/std", "aurora-engine-transactions/std", "ethabi/std", "aurora-evm/std", "hex/std", "rlp/std", "serde/std", "serde_json/std"]
contract = ["log", "aurora-engine-sdk/contract", "aurora-engine-precompiles/contract"]
log = ["aurora-engine-sdk/log", "aurora-engine-precompiles/log"]
tracing = ["aurora-evm/tracing"]
error_refund = ["aurora-engine-precompiles/error_refund"]
integration-test = ["log"]
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

**File:** engine-tests/src/tests/erc20_connector.rs (L623-666)
```rust
    #[tokio::test]
    async fn test_exit_to_near_refund() {
        // Deploy Aurora; deploy NEP-141; bridge NEP-141 to ERC-20 on Aurora
        let TestExitToNearContext {
            ft_owner,
            ft_owner_address,
            nep_141,
            erc20,
            aurora,
            ..
        } = test_exit_to_near_common().await.unwrap();

        // Call exit on ERC-20; ft_transfer promise fails; expect refund on Aurora;
        exit_to_near(
            &ft_owner,
            // The ft_transfer will fail because this account is not registered with the NEP-141
            "unregistered.near",
            FT_EXIT_AMOUNT,
            &erc20,
            &aurora,
        )
        .await
        .unwrap();

        assert_eq!(
            nep_141_balance_of(&nep_141, &ft_owner.id()).await,
            FT_TOTAL_SUPPLY - FT_TRANSFER_AMOUNT
        );
        assert_eq!(
            nep_141_balance_of(&nep_141, &aurora.id()).await,
            FT_TRANSFER_AMOUNT
        );

        #[cfg(feature = "error_refund")]
        let balance = FT_TRANSFER_AMOUNT.into();
        // If the refund feature is not enabled then there is no refund in the EVM
        #[cfg(not(feature = "error_refund"))]
        let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();

        assert_eq!(
            erc20_balance(&erc20, ft_owner_address, &aurora).await,
            balance
        );
    }
```
