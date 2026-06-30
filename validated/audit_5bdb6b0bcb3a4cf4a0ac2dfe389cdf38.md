### Title
Missing Refund Callback in `ExitToEthereum` Precompile Causes Permanent ETH Loss on `withdraw` Promise Failure — (`engine-precompiles/src/native.rs`)

---

### Summary

The `ExitToEthereum` precompile burns a user's ETH from the EVM state and schedules a fire-and-forget `withdraw` cross-contract call to the ETH connector with **no error callback**. If the `withdraw` promise fails for any reason, the user's ETH is permanently destroyed with no recovery path. This is structurally identical to M-02: funds are committed before the settlement step completes, and there is no mechanism to replenish or refund them on failure.

---

### Finding Description

In `engine-precompiles/src/native.rs`, `ExitToEthereum::run` (lines 977–1003):

1. The EVM execution context deducts `context.apparent_value` (ETH) from the caller's EVM balance as part of the call.
2. A `withdraw` promise is constructed and wrapped as `PromiseArgs::Create` — a bare, one-way promise with no callback.
3. The promise log is emitted and the function returns. There is no `PromiseArgs::Callback` attached.

```rust
let withdraw_promise = PromiseCreateArgs {
    target_account_id: nep141_address,
    method: "withdraw".to_string(),
    args: serialized_args,
    attached_balance: Yocto::new(1),
    attached_gas: costs::WITHDRAWAL_GAS,
};

let promise = borsh::to_vec(&PromiseArgs::Create(withdraw_promise)).unwrap();
``` [1](#0-0) 

Compare this to `ExitToNear::run` (lines 470–483), which conditionally wraps the transfer promise in a `PromiseArgs::Callback` pointing to `exit_to_near_precompile_callback`:

```rust
let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
    PromiseArgs::Create(transfer_promise)
} else {
    PromiseArgs::Callback(PromiseWithCallbackArgs {
        base: transfer_promise,
        callback: PromiseCreateArgs {
            target_account_id: self.current_account_id.clone(),
            method: "exit_to_near_precompile_callback".to_string(),
            ...
        },
    })
};
``` [2](#0-1) 

The `exit_to_near_precompile_callback` in `engine/src/contract_methods/connector.rs` (lines 231–239) calls `engine::refund_on_error` to restore the user's tokens when the base promise fails:

```rust
} else if let Some(args) = args.refund {
    // Exit call failed; need to refund tokens
    let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;
    ...
}
``` [3](#0-2) 

`ExitToEthereum` has **no equivalent callback and no refund path whatsoever**, regardless of any feature flag.

---

### Impact Explanation

**Critical — Permanent freezing of user funds.**

When a user calls `ExitToEthereum`:
- Their ETH is atomically deducted from the EVM state during the `submit` call.
- The `withdraw` promise is scheduled to execute in a subsequent NEAR block.
- If the `withdraw` promise fails (for any reason), the ETH has already been removed from the EVM state and no withdrawal proof is ever created on the ETH connector side.
- The ETH is permanently destroyed: it no longer exists in the EVM, and it never arrives at the Ethereum destination.

There is no admin function, no callback, and no re-entry path to recover these funds. The contract cannot be "replenished" because the EVM balance was burned, not transferred.

---

### Likelihood Explanation

The `withdraw` promise can fail under realistic, non-malicious conditions:

1. **Connector paused**: The ETH connector owner calls `pa_pause_feature` for maintenance. Any in-flight `ExitToEthereum` whose `withdraw` promise executes while the connector is paused will fail silently. The test suite explicitly demonstrates this pause mechanism (`test_withdraw_from_near_pausability`). [4](#0-3) 

2. **Connector account changed**: `set_eth_connector_contract_account` redirects the connector account ID. If changed between the EVM execution and the promise execution, the `withdraw` is sent to the wrong account and fails. [5](#0-4) 

3. **Any panic or error in the connector contract**: Since there is no callback, any failure in the connector's `withdraw` function is silently swallowed.

The test `test_exit_to_near_eth_refund` explicitly demonstrates that the analogous `ExitToNear` path handles this failure by refunding the user — confirming the developers are aware of the failure mode but did not apply the same protection to `ExitToEthereum`. [6](#0-5) 

---

### Recommendation

Add a callback to `ExitToEthereum` analogous to `exit_to_near_precompile_callback`. The callback should:
1. Check the result of the `withdraw` promise.
2. On failure, call `engine::refund_on_error` to restore the user's ETH (for the base token case) or re-mint the burned ERC-20 tokens (for the ERC-20 case).

The `refund_on_error` function already handles both cases: [7](#0-6) 

The `ExitToEthereum` promise construction should be changed from `PromiseArgs::Create` to `PromiseArgs::Callback` with a new `exit_to_ethereum_precompile_callback` method registered on the engine contract.

---

### Proof of Concept

**Step 1**: User submits an EVM transaction calling the `ExitToEthereum` precompile with `flag = 0x00` and a valid Ethereum recipient address. The EVM deducts `amount` ETH from the user's balance. A `withdraw` promise is scheduled to the ETH connector.

**Step 2**: Before the `withdraw` promise executes (next NEAR block), the ETH connector owner calls `pa_pause_feature("engine_withdraw")`.

**Step 3**: The `withdraw` promise executes against the paused connector and fails.

**Step 4**: No callback exists. The failure is not observed by the engine. The user's ETH is gone from the EVM state and no withdrawal proof was created. The ETH is permanently lost.

The asymmetry is confirmed by the codebase itself: `ExitToNear` wraps its promise in a `PromiseArgs::Callback` with `exit_to_near_precompile_callback` when the `error_refund` feature is enabled, while `ExitToEthereum` unconditionally uses `PromiseArgs::Create` with no callback under any build configuration. [8](#0-7) [2](#0-1)

### Citations

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

**File:** engine-precompiles/src/native.rs (L977-990)
```rust
        let withdraw_promise = PromiseCreateArgs {
            target_account_id: nep141_address,
            method: "withdraw".to_string(),
            args: serialized_args,
            attached_balance: Yocto::new(1),
            attached_gas: costs::WITHDRAWAL_GAS,
        };

        let promise = borsh::to_vec(&PromiseArgs::Create(withdraw_promise)).unwrap();
        let promise_log = Log {
            address: exit_to_ethereum::ADDRESS.raw(),
            topics: Vec::new(),
            data: promise,
        };
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

**File:** engine/src/contract_methods/connector.rs (L418-438)
```rust
pub fn set_eth_connector_contract_account<I: IO + Copy, E: Env>(
    io: I,
    env: &E,
) -> Result<(), ContractError> {
    with_hashchain(io, env, function_name!(), |io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;
        let is_private = env.assert_private_call();

        if is_private.is_err() {
            require_owner_only(&state, &env.predecessor_account_id())?;
        }

        let args: SetEthConnectorContractAccountArgs = io.read_input_borsh()?;

        set_connector_account_id(io, &args.account);
        set_connector_withdraw_serialization_type(io, &args.withdraw_serialize_type);

        Ok(())
    })
}
```

**File:** engine-tests-connector/src/connector.rs (L478-530)
```rust
#[tokio::test]
async fn test_withdraw_from_near_pausability() -> anyhow::Result<()> {
    let contract = TestContract::new_with_owner("owner").await?;
    let user_acc = contract
        .create_sub_account(DEPOSITED_RECIPIENT_NAME)
        .await?;
    let res = contract
        .deposit_eth_to_near(user_acc.id(), DEPOSITED_AMOUNT.into())
        .await?;
    assert!(res.is_success(), "{res:#?}");
    let res = contract
        .deposit_eth_to_near(
            contract.owner.as_ref().unwrap().id(),
            DEPOSITED_AMOUNT.into(),
        )
        .await?;
    assert!(res.is_success(), "{res:#?}");

    let pause_args = json!({"key": "engine_withdraw"});

    let withdraw_amount = NEP141Wei::new(100);
    // 1st withdraw - should succeed
    let res = user_acc
        .call(contract.engine_contract.id(), "withdraw")
        .args_borsh((*RECIPIENT_ADDRESS, withdraw_amount))
        .max_gas()
        .deposit(ONE_YOCTO)
        .transact()
        .await?;
    assert!(res.is_success());

    // Pause withdraw
    let res = contract
        .owner
        .as_ref()
        .unwrap()
        .call(contract.eth_connector_contract.id(), "pa_pause_feature")
        .args_json(&pause_args)
        .max_gas()
        .transact()
        .await?;
    assert!(res.is_success(), "{res:#?}");

    // 2nd withdraw - should be failed
    let res = user_acc
        .call(contract.engine_contract.id(), "withdraw")
        .args_borsh((*RECIPIENT_ADDRESS, withdraw_amount))
        .max_gas()
        .deposit(ONE_YOCTO)
        .transact()
        .await?;
    assert!(res.is_failure());
    assert!(contract.check_error_message(&res, "Pausable: Method is paused")?);
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

**File:** engine/src/engine.rs (L1176-1225)
```rust
pub fn refund_on_error<I: IO + Copy, E: Env, P: PromiseHandler>(
    io: I,
    env: &E,
    state: EngineState,
    args: &RefundCallArgs,
    handler: &mut P,
) -> EngineResult<SubmitResult> {
    let current_account_id = env.current_account_id();
    if let Some(erc20_address) = args.erc20_address {
        // ERC-20 exit; re-mint burned tokens
        let erc20_admin_address = current_address(&current_account_id);
        let mut engine: Engine<_, _> =
            Engine::new_with_state(state, erc20_admin_address, current_account_id, io, env);

        let refund_address = args.recipient_address;
        let amount = U256::from_big_endian(&args.amount);
        let input = setup_refund_on_error_input(amount, refund_address);

        engine.call(
            &erc20_admin_address,
            &erc20_address,
            Wei::zero(),
            input,
            u64::MAX,
            Vec::new(),
            Vec::new(),
            handler,
        )
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
}
```
