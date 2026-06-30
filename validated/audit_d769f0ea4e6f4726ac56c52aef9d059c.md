### Title
Permanent Fund Freeze When NEAR-Side `ft_transfer` Fails in `ExitToNear` Precompile Without `error_refund` Feature — (`engine-precompiles/src/native.rs`)

---

### Summary

When the `error_refund` compile-time feature is absent (it is **not** in the `default` feature set), the `ExitToNear` precompile irrevocably burns a user's ETH or ERC-20 tokens on the EVM side before scheduling a NEAR-side `ft_transfer`. If that NEAR promise fails, no refund path exists and the tokens are permanently frozen.

---

### Finding Description

The `ExitToNear` precompile (`engine-precompiles/src/native.rs`) deducts the user's EVM balance atomically during EVM execution, then emits a NEAR promise log that the engine later schedules as an async cross-contract call.

The `refund` field of `ExitToNearPrecompileCallbackArgs` is populated only when the `error_refund` feature is compiled in: [1](#0-0) 

`error_refund` is **not** listed under `default` in either `engine/Cargo.toml` or `engine-precompiles/Cargo.toml`: [2](#0-1) [3](#0-2) 

Without `error_refund`, for a plain ETH or ERC-20 exit, both `refund` and `transfer_near` are `None`, so `callback_args` equals `ExitToNearPrecompileCallbackArgs::default()` and the engine schedules the NEAR promise **without any callback**: [4](#0-3) 

If the NEAR-side `ft_transfer` fails (e.g., the recipient is not registered with the NEP-141 contract), there is no callback to detect the failure. For the ERC-20 case a callback is attached, but its failure branch is a no-op without `error_refund`: [5](#0-4) 

The EVM-side burn has already committed; the tokens are permanently lost.

The test suite explicitly acknowledges this behavior: [6](#0-5) [7](#0-6) 

The comment in `exit_erc20_token_to_near` itself warns that ETH attached to an ERC-20 exit would be locked forever in the precompile address — the same class of risk applies to the token amount when the NEAR promise fails: [8](#0-7) 

---

### Impact Explanation

**Critical — Permanent freezing of funds.**

Any ETH or bridged ERC-20 tokens sent through the `ExitToNear` precompile are burned from the EVM ledger before the NEAR-side transfer is attempted. If that transfer fails and `error_refund` is not compiled in, the tokens are gone with no on-chain recovery path. The precompile address (`0xe9217bc70b7ed1f598ddd3199e80b093fa71124f`) holds no spendable code; the EVM balance credited there cannot be moved by any user transaction.

---

### Likelihood Explanation

**Medium.** The trigger condition — a NEAR-side `ft_transfer` failing — is realistic and user-controllable. The test `test_exit_to_near_refund` demonstrates it by sending to `"unregistered.near"`, an account not registered with the NEP-141 contract. Any user who exits ERC-20 tokens to a NEAR account that has not called `storage_deposit` on the corresponding NEP-141 will hit this path. Because `error_refund` is not a default feature, a standard production build is vulnerable.

---

### Recommendation

1. Promote `error_refund` to a **default feature** in both `engine/Cargo.toml` and `engine-precompiles/Cargo.toml` so that the refund callback is always compiled into production builds.
2. Alternatively, make the refund path unconditional in `exit_to_near_precompile_callback` and remove the compile-time gate entirely.
3. Document explicitly which production deployments have `error_refund` enabled and what happens to user funds when the NEAR-side transfer fails in deployments where it is absent.

---

### Proof of Concept

1. Deploy Aurora Engine **without** the `error_refund` feature (the default).
2. Bridge a NEP-141 token to Aurora as an ERC-20.
3. From an EVM address that holds ERC-20 tokens, call the ERC-20's `withdrawToNear` targeting `"unregistered.near"` (an account that has not called `storage_deposit` on the NEP-141).
4. The ERC-20 tokens are burned from the EVM balance immediately.
5. The NEAR-side `ft_transfer` fails because `"unregistered.near"` is not registered.
6. No callback fires a refund; the ERC-20 balance is permanently reduced.
7. Confirmed by the existing test `test_exit_to_near_refund` which asserts `balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT)` when `error_refund` is absent — the exit amount is gone with no recovery. [9](#0-8)

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

**File:** engine-precompiles/src/native.rs (L572-580)
```rust
    // In case of withdrawing ERC-20 tokens, the `apparent_value` should be zero. In opposite way
    // the funds will be locked in the address of the precompile without any possibility
    // to withdraw them in the future. So, in case if the `apparent_value` is not zero, the error
    // will be returned to prevent that.
    if context.apparent_value != U256::zero() {
        return Err(ExitError::Other(Cow::from(
            "ERR_ETH_ATTACHED_FOR_ERC20_EXIT",
        )));
    }
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

**File:** engine-precompiles/Cargo.toml (L34-39)
```text
[features]
default = ["std"]
std = ["aurora-engine-types/std", "aurora-engine-sdk/bls", "aurora-engine-sdk/std", "aurora-engine-modexp/std", "aurora-evm/std", "ethabi/std", "serde/std", "serde_json/std"]
contract = ["aurora-engine-sdk/contract", "aurora-engine-sdk/bls"]
log = []
error_refund = []
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

**File:** engine-tests/src/tests/erc20_connector.rs (L623-665)
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
```

**File:** engine-tests/src/tests/erc20_connector.rs (L771-775)
```rust
        #[cfg(feature = "error_refund")]
        let expected_balance = Wei::new_u64(INITIAL_ETH_BALANCE);
        // If the refund feature is not enabled, then there is no refund in the EVM
        #[cfg(not(feature = "error_refund"))]
        let expected_balance = Wei::new_u64(INITIAL_ETH_BALANCE - ETH_EXIT_AMOUNT);
```
