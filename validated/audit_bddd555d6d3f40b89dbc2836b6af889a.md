### Title
Missing ETH Refund Path in `ExitToNear` Precompile When `error_refund` Feature Is Disabled — (File: `engine-precompiles/src/native.rs`)

---

### Summary

When the `error_refund` Cargo feature is not compiled in, the `ExitToNear` precompile schedules no callback after a failed `ft_transfer` promise. ETH deducted from the caller's EVM balance and credited to the `exit_to_near::ADDRESS` precompile address is permanently frozen with no on-chain recovery path — a direct structural analog to the missing `distributeETH` gap in the reference report.

---

### Finding Description

**Root cause — `engine-precompiles/src/native.rs`, `ExitToNear::run()`:**

When a user calls the `ExitToNear` precompile with ETH attached (`context.apparent_value > 0`), the EVM deducts that ETH from the caller's balance and credits it to `exit_to_near::ADDRESS`. The precompile then builds a `callback_args` struct:

```rust
let callback_args = ExitToNearPrecompileCallbackArgs {
    #[cfg(feature = "error_refund")]
    refund: refund_call_args(&exit_to_near_params, &exit_event),
    #[cfg(not(feature = "error_refund"))]
    refund: None,
    transfer_near: transfer_near_args,
};
``` [1](#0-0) 

For the base-ETH exit path, `transfer_near_args` is also `None` (returned by `exit_base_token_to_near`), so without `error_refund`:

```rust
let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
    PromiseArgs::Create(transfer_promise)   // ← no callback attached
} else { ... }
``` [2](#0-1) 

Only a bare `ft_transfer` promise is scheduled — no `exit_to_near_precompile_callback` is ever attached. If that `ft_transfer` fails (e.g., recipient not registered with the NEP-141 token), the ETH remains at `exit_to_near::ADDRESS` in the EVM state with no mechanism to move it out.

The recovery function `refund_on_error` in `engine/src/engine.rs` explicitly transfers ETH back from `exit_to_near::ADDRESS` to the user:

```rust
// ETH exit; transfer ETH back from precompile address
let exit_address = exit_to_near::ADDRESS;
engine.call(&exit_address, &refund_address, amount, ...)
``` [3](#0-2) 

But `refund_on_error` is only reachable through the callback that is **never scheduled** when `error_refund` is absent. `exit_to_near::ADDRESS` is a precompile address — no EVM contract code exists there, so no user-initiated call can drain it.

The test suite explicitly acknowledges the freeze:

```rust
// If the refund feature is not enabled, then there is no refund in the EVM
#[cfg(not(feature = "error_refund"))]
let expected_balance = Wei::new_u64(INITIAL_ETH_BALANCE - ETH_EXIT_AMOUNT);
``` [4](#0-3) 

---

### Impact Explanation

**Critical — Permanent freezing of funds.**

Any ETH sent through `ExitToNear` whose downstream `ft_transfer` fails is irrecoverably frozen at `exit_to_near::ADDRESS` in the Aurora EVM state. Because `exit_to_near::ADDRESS` is a precompile address with no deployable bytecode, no EVM transaction can transfer ETH out of it. The Aurora Engine is upgradeable as a NEAR contract, but that requires a governance action; the funds are frozen until such an upgrade is deliberately crafted and deployed.

---

### Likelihood Explanation

**Medium.**

The `ft_transfer` to the ETH connector fails whenever the recipient NEAR account is not registered with the NEP-141 token (a common condition for new accounts). Any unprivileged EVM user can trigger the freeze by calling `ExitToNear` with ETH attached and supplying any unregistered NEAR account ID as the recipient. The entry path is fully user-controlled via the standard `submit` method on the Aurora Engine contract. [5](#0-4) 

---

### Recommendation

1. **Enable `error_refund` in the production build** so that `refund_call_args` is populated and the `exit_to_near_precompile_callback` is always attached as a callback to the `ft_transfer` promise.
2. Alternatively, unconditionally attach the callback for the ETH base-token exit path regardless of the feature flag, mirroring the `refund_on_error` logic already present in `engine/src/engine.rs`.

---

### Proof of Concept

1. Deploy Aurora Engine **without** the `error_refund` feature.
2. Fund a signer address with ETH on Aurora (e.g., `INITIAL_ETH_BALANCE = 777_777_777`).
3. Drain Aurora's NEP-141 balance so the `ft_transfer` will fail (as done in `test_exit_to_near_eth_refund`).
4. Submit an EVM transaction calling `ExitToNear` with `ETH_EXIT_AMOUNT` attached and any NEAR account ID as recipient.
5. Observe: the `ft_transfer` promise fails; no callback fires; the signer's EVM balance is permanently reduced by `ETH_EXIT_AMOUNT`; `exit_to_near::ADDRESS` holds the frozen ETH with no recovery path. [6](#0-5)

### Citations

**File:** engine-precompiles/src/native.rs (L430-433)
```rust
                ExitToNearParams::BaseToken(ref exit_params) => {
                    let eth_connector_account_id = self.get_eth_connector_contract_account()?;
                    exit_base_token_to_near(eth_connector_account_id, context, exit_params)?
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

**File:** engine/src/engine.rs (L1204-1224)
```rust
    } else {
        // ETH exit; transfer ETH back from precompile address
        let exit_address = exit_to_near::ADDRESS;
        let mut engine: Engine<_, _> =
            Engine::new_with_state(state, exit_address, current_account_id, io, env);
        let refund_address = args.recipient_address;
        let amount = Wei::new(U256::from_big_endian(&args.amount));
        engine.call(
            &exit_address,
            &refund_address,
            amount,
            Vec::new(),
            u64::MAX,
            vec![
                (exit_address.raw(), Vec::new()),
                (refund_address.raw(), Vec::new()),
            ],
            Vec::new(),
            handler,
        )
    }
```

**File:** engine-tests/src/tests/erc20_connector.rs (L717-781)
```rust
    #[tokio::test]
    async fn test_exit_to_near_eth_refund() {
        // Test the case where the ft_transfer promise from the exit call fails;
        // ensure ETH is refunded.

        let TestExitToNearEthContext {
            signer,
            signer_address,
            chain_id,
            tester_address,
            aurora,
        } = test_exit_to_near_eth_common().await.unwrap();
        let exit_account_id = "any.near";

        // Make the ft_transfer call fail by draining the Aurora account
        let result = aurora
            .ft_transfer(
                &"tmp.near".parse().unwrap(),
                u128::from(INITIAL_ETH_BALANCE).into(),
                &None,
            )
            .max_gas()
            .deposit(NearToken::from_yoctonear(1))
            .transact()
            .await
            .unwrap();
        assert!(result.is_success());

        // call exit to near
        let input = build_input(
            "withdrawEthToNear(bytes)",
            &[ethabi::Token::Bytes(exit_account_id.as_bytes().to_vec())],
        );
        let tx = utils::create_eth_transaction(
            Some(tester_address),
            Wei::new_u64(ETH_EXIT_AMOUNT),
            input,
            Some(chain_id),
            &signer.secret_key,
        );
        let result = aurora
            .submit(rlp::encode(&tx).to_vec())
            .max_gas()
            .transact()
            .await
            .unwrap();
        assert!(result.is_success());

        // check balances
        assert_eq!(
            nep_141_balance_of(aurora.as_raw_contract(), &exit_account_id.parse().unwrap()).await,
            0
        );

        #[cfg(feature = "error_refund")]
        let expected_balance = Wei::new_u64(INITIAL_ETH_BALANCE);
        // If the refund feature is not enabled, then there is no refund in the EVM
        #[cfg(not(feature = "error_refund"))]
        let expected_balance = Wei::new_u64(INITIAL_ETH_BALANCE - ETH_EXIT_AMOUNT);

        assert_eq!(
            eth_balance_of(signer_address, &aurora).await,
            expected_balance
        );
    }
```
