### Title
Permanent ETH Freeze When `ExitToNear` Precompile's Downstream `ft_transfer` Promise Fails Without `error_refund` Feature - (`engine-precompiles/src/native.rs`)

---

### Summary

When the `error_refund` compile-time feature is not enabled (the default production build), the `ExitToNear` precompile deducts ETH from the user's EVM balance and schedules a NEAR `ft_transfer` promise with **no failure callback**. If that promise fails, the ETH is permanently lost: it has been burned from the EVM state but never credited to the NEAR recipient, and there is no recovery path.

---

### Finding Description

The `ExitToNear` precompile's `run` function in `engine-precompiles/src/native.rs` constructs a `callback_args` struct that conditionally includes a `refund` field:

```rust
let callback_args = ExitToNearPrecompileCallbackArgs {
    #[cfg(feature = "error_refund")]
    refund: refund_call_args(&exit_to_near_params, &exit_event),
    #[cfg(not(feature = "error_refund"))]
    refund: None,
    transfer_near: transfer_near_args,
};
``` [1](#0-0) 

For the ETH base token exit path (`ExitToNearParams::BaseToken`), `exit_base_token_to_near` returns `None` for `transfer_near_args` in both the legacy `ft_transfer` and `ft_transfer_call` branches: [2](#0-1) 

When `error_refund` is not enabled, both `refund` and `transfer_near` are `None`, making `callback_args == ExitToNearPrecompileCallbackArgs::default()`. The code then takes the no-callback branch:

```rust
let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
    PromiseArgs::Create(transfer_promise)   // ← no callback scheduled
} else {
    PromiseArgs::Callback(PromiseWithCallbackArgs { ... })
};
``` [3](#0-2) 

The `ft_transfer` NEAR promise is dispatched with no attached callback. If it fails (e.g., recipient account not registered with the NEP-141 contract), the NEAR runtime silently discards the failure. The ETH that was already deducted from the user's EVM balance is permanently gone.

The `exit_to_near_precompile_callback` function, which is the only place `refund_on_error` is called, is never scheduled in this path: [4](#0-3) 

The `refund_on_error` function in `engine/src/engine.rs` is the correct recovery mechanism — it re-credits ETH from the precompile address back to the user — but it is unreachable in this code path: [5](#0-4) 

The existing test suite explicitly acknowledges this loss as a known behavior when the feature is absent:

```rust
// If the refund feature is not enabled, then there is no refund in the EVM
#[cfg(not(feature = "error_refund"))]
let expected_balance = Wei::new_u64(INITIAL_ETH_BALANCE - ETH_EXIT_AMOUNT);
``` [6](#0-5) 

The same pattern applies to the ERC-20 exit path when `ft_transfer` fails without `error_refund`: burned ERC-20 tokens are not re-minted. [7](#0-6) 

---

### Impact Explanation

**Permanent freezing of funds.** When a user calls the `ExitToNear` precompile with ETH value and the downstream `ft_transfer` NEAR promise fails, the ETH is deducted from the user's EVM balance and never returned. There is no administrative recovery path: the ETH sits at the `exit_to_near::ADDRESS` precompile address with no mechanism to withdraw it. The same applies to ERC-20 tokens that are burned before the failed `ft_transfer`.

---

### Likelihood Explanation

The failure condition is realistic and user-triggerable:

1. A NEAR account must be registered with a NEP-141 contract before it can receive tokens via `ft_transfer`. Any user who specifies an unregistered NEAR account ID as the recipient will trigger the failure.
2. The `ft_transfer` call can also fail if the Aurora engine account has insufficient NEP-141 balance (e.g., due to a race condition or accounting discrepancy).
3. No special privileges are required. Any EVM address can call the `ExitToNear` precompile (at `exit_to_near::ADDRESS`) by sending an EVM transaction with ETH value.

The `error_refund` feature is not listed as a default feature in any `Cargo.toml` in the workspace, meaning the production WASM build ships without it. [8](#0-7) 

---

### Recommendation

1. **Enable `error_refund` by default** in the production build, or unconditionally include the refund callback for all exit paths.
2. If the feature flag must remain, ensure the ETH base token exit path always schedules a callback with a populated `refund` field so that `exit_to_near_precompile_callback` can invoke `refund_on_error` on failure.
3. Alternatively, validate that the recipient NEAR account is registered before deducting the user's EVM balance, reverting the EVM transaction if the precondition cannot be confirmed synchronously.

---

### Proof of Concept

1. Deploy Aurora Engine **without** the `error_refund` feature (the default).
2. Fund an EVM address with ETH on Aurora.
3. Submit an EVM transaction calling the `ExitToNear` precompile (`exit_to_near::ADDRESS`) with `apparent_value > 0` and a NEAR recipient account ID that is **not registered** with the ETH connector NEP-141 contract.
4. Observe: the EVM transaction succeeds, the user's EVM ETH balance is reduced by the exit amount, the NEAR `ft_transfer` promise fails silently, and the user's ETH balance is never restored.

This is directly confirmed by the existing test `test_exit_to_near_eth_refund` in `engine-tests/src/tests/erc20_connector.rs` (lines 717–781), which asserts `INITIAL_ETH_BALANCE - ETH_EXIT_AMOUNT` as the post-failure balance when `error_refund` is absent — proving the ETH is permanently lost. [9](#0-8)

### Citations

**File:** engine-precompiles/src/native.rs (L36-39)
```rust
#[cfg(not(feature = "error_refund"))]
const MIN_INPUT_SIZE: usize = 3;
#[cfg(feature = "error_refund")]
const MIN_INPUT_SIZE: usize = 21;
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

**File:** engine-precompiles/src/native.rs (L504-556)
```rust
fn exit_base_token_to_near(
    eth_connector_account_id: AccountId,
    context: &Context,
    exit_params: &BaseTokenParams,
) -> Result<
    (
        AccountId,
        String,
        events::ExitToNear,
        String,
        Option<TransferNearArgs>,
    ),
    ExitError,
> {
    match exit_params.message {
        Some(Message::Omni(msg)) => Ok((
            eth_connector_account_id,
            ft_transfer_call_args(
                &exit_params.receiver_account_id,
                context.apparent_value,
                msg,
            )?,
            events::ExitToNear::Omni(ExitToNearOmni {
                sender: Address::new(context.caller),
                erc20_address: events::ETH_ADDRESS,
                dest: exit_params.receiver_account_id.to_string(),
                amount: context.apparent_value,
                msg: msg.to_string(),
            }),
            "ft_transfer_call".to_string(),
            None,
        )),
        None => Ok((
            eth_connector_account_id,
            // There is no way to inject json, given the encoding of both arguments
            // as decimal and valid account id respectively.
            format!(
                r#"{{"receiver_id":"{}","amount":"{}"}}"#,
                exit_params.receiver_account_id,
                context.apparent_value.as_u128()
            ),
            events::ExitToNear::Legacy(ExitToNearLegacy {
                sender: Address::new(context.caller),
                erc20_address: events::ETH_ADDRESS,
                dest: exit_params.receiver_account_id.to_string(),
                amount: context.apparent_value,
            }),
            "ft_transfer".to_string(),
            None,
        )),
        _ => Err(ExitError::Other(Cow::from("ERR_INVALID_MESSAGE"))),
    }
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

**File:** engine-tests/src/tests/erc20_connector.rs (L656-660)
```rust
        #[cfg(feature = "error_refund")]
        let balance = FT_TRANSFER_AMOUNT.into();
        // If the refund feature is not enabled then there is no refund in the EVM
        #[cfg(not(feature = "error_refund"))]
        let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();
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
