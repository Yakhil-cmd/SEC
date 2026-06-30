### Title
ExitToEthereum Precompile Has No Error Refund Callback, Causing Permanent Token Loss When Withdrawal Fails - (File: engine-precompiles/src/native.rs)

### Summary
The `ExitToEthereum` precompile burns user tokens (ETH or ERC-20) inside the EVM and then schedules a NEAR-level promise to call `withdraw` on the eth-connector. Unlike `ExitToNear`, which has an `exit_to_near_precompile_callback` error-recovery path, `ExitToEthereum` attaches **no callback**. If the `withdraw` promise fails for any reason (e.g., the eth-connector's `withdraw` feature is paused), the tokens are already committed as burned in the EVM with no refund mechanism.

### Finding Description
In `engine-precompiles/src/native.rs`, `ExitToEthereum::run` constructs a bare `PromiseArgs::Create` with no callback attached:

```rust
// lines 977–985
let withdraw_promise = PromiseCreateArgs {
    target_account_id: nep141_address,
    method: "withdraw".to_string(),
    args: serialized_args,
    attached_balance: Yocto::new(1),
    attached_gas: costs::WITHDRAWAL_GAS,
};
let promise = borsh::to_vec(&PromiseArgs::Create(withdraw_promise)).unwrap();
``` [1](#0-0) 

By contrast, `ExitToNear::run` conditionally wraps its transfer promise in a `PromiseArgs::Callback` that targets `exit_to_near_precompile_callback` on the engine, which can refund tokens on failure:

```rust
// lines 470–483
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

The `exit_to_near_precompile_callback` handler in `engine/src/contract_methods/connector.rs` (lines 196–246) processes the promise result and, when the `error_refund` feature is active, calls `engine::refund_on_error` to restore burned tokens. [3](#0-2) 

For `ExitToEthereum` there is no equivalent. The EVM state change (token burn) is committed as part of the EVM transaction before the NEAR promise is dispatched. NEAR promise failures are silent when no callback is registered, so a failed `withdraw` call leaves the user with burned tokens and no recourse.

The eth-connector exposes a `pa_pause_feature` mechanism. The test `test_withdraw_from_near_pausability` demonstrates that `engine_withdraw` (the method the eth-connector's `withdraw` ultimately invokes) can be paused by the owner: [4](#0-3) 

When `withdraw` is paused on the eth-connector, any in-flight `ExitToEthereum` promise will fail, and the burned tokens are irrecoverable.

### Impact Explanation
**Critical – Permanent freezing of funds.** Tokens burned in the EVM during `ExitToEthereum` cannot be recovered if the downstream `withdraw` promise fails. There is no alternative path: unlike the external report's 1INCH fallback, Aurora provides no secondary mechanism to reclaim burned ETH or ERC-20 tokens.

### Likelihood Explanation
**Medium.** The eth-connector's `withdraw` feature is a legitimate operational control that can be paused for maintenance or emergency response. Any user who calls `ExitToEthereum` during a pause window permanently loses their tokens. This is an ordinary operational scenario, not an exotic attack.

### Recommendation
Add an error-recovery callback to `ExitToEthereum` analogous to `exit_to_near_precompile_callback`. The callback should inspect the promise result and, on failure, re-mint or refund the burned tokens to the original sender. The `ExitToNearPrecompileCallbackArgs` / `refund_on_error` infrastructure in `engine/src/contract_methods/connector.rs` and `engine/src/engine.rs` already provides the pattern to follow.

### Proof of Concept
1. The eth-connector owner calls `pa_pause_feature` with key `"engine_withdraw"` (or the equivalent `withdraw` key on the eth-connector), pausing withdrawals for maintenance.
2. An unprivileged Aurora user calls `ExitToEthereum` (flag `0x00`) to bridge ETH to Ethereum. The EVM deducts ETH from the user's balance and emits a promise log targeting `withdraw` on the eth-connector.
3. The EVM transaction commits; the user's ETH balance is now zero.
4. The NEAR runtime dispatches the `withdraw` promise. The eth-connector rejects it because the feature is paused (`"Pausable: Method is paused"`).
5. No callback is registered on the promise, so the failure is silently dropped.
6. The user's ETH is permanently burned. There is no `withdrawWETHInEtherfi`-equivalent recovery path — the only exit was the now-failed bridge call. [5](#0-4)

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

**File:** engine-precompiles/src/native.rs (L844-1000)
```rust
impl<I: IO> Precompile for ExitToEthereum<I> {
    fn required_gas(_input: &[u8]) -> Result<EthGas, ExitError> {
        Ok(costs::EXIT_TO_ETHEREUM_GAS)
    }

    #[allow(clippy::too_many_lines)]
    fn run(
        &self,
        input: &[u8],
        target_gas: Option<EthGas>,
        context: &Context,
        is_static: bool,
    ) -> EvmPrecompileResult {
        // ETH (Base token) transfer input format (min size 21 bytes)
        //  - flag (1 byte)
        //  - eth_recipient (20 bytes)
        // ERC-20 transfer input format: max 53 bytes
        //  - flag (1 byte)
        //  - amount (32 bytes)
        //  - eth_recipient (20 bytes)
        validate_input_size(input, 21, 53)?;

        let required_gas = Self::required_gas(input)?;

        if let Some(target_gas) = target_gas
            && required_gas > target_gas
        {
            return Err(ExitError::OutOfGas);
        }

        // It's not allowed to call exit precompiles in static mode
        if is_static {
            return Err(ExitError::Other(Cow::from("ERR_INVALID_IN_STATIC")));
        } else if context.address != exit_to_ethereum::ADDRESS.raw() {
            return Err(ExitError::Other(Cow::from("ERR_INVALID_IN_DELEGATE")));
        }

        // The first byte of the input is a flag, selecting the behavior to be triggered:
        //  0x00 -> ETH (Base token) token transfer
        //  0x01 -> ERC-20 transfer
        let mut input = input;
        let flag = input[0];
        input = &input[1..];

        let (nep141_address, serialized_args, exit_event) = match flag {
            0x0 => {
                // ETH (base) transfer
                //
                // Input slice format:
                //  eth_recipient (20 bytes) - the address of recipient which will receive ETH on Ethereum
                let recipient_address: Address = input
                    .try_into()
                    .map_err(|_| ExitError::Other(Cow::from("ERR_INVALID_RECIPIENT_ADDRESS")))?;
                let serialize_fn = match get_withdraw_serialize_type(&self.io)? {
                    WithdrawSerializeType::Json => json_args,
                    WithdrawSerializeType::Borsh => borsh_args,
                };
                let eth_connector_account_id = self.get_eth_connector_contract_account()?;

                (
                    eth_connector_account_id,
                    // There is no way to inject json, given the encoding of both arguments
                    // as decimal and hexadecimal respectively.
                    serialize_fn(recipient_address, context.apparent_value)?,
                    events::ExitToEth {
                        sender: Address::new(context.caller),
                        erc20_address: events::ETH_ADDRESS,
                        dest: recipient_address,
                        amount: context.apparent_value,
                    },
                )
            }
            0x1 => {
                // ERC-20 transfer
                //
                // This precompile branch is expected to be called from the ERC20 withdraw function
                // (or burn function with some flag provided that this is expected to be withdrawn)
                //
                // Input slice format:
                //  amount (U256 big-endian bytes) - the amount that was burned
                //  eth_recipient (20 bytes) - the address of recipient which will receive ETH on Ethereum

                if context.apparent_value != U256::from(0) {
                    return Err(ExitError::Other(Cow::from(
                        "ERR_ETH_ATTACHED_FOR_ERC20_EXIT",
                    )));
                }

                let erc20_address = context.caller;
                let nep141_address = get_nep141_from_erc20(erc20_address.as_bytes(), &self.io)?;
                let amount = parse_amount(&input[..32])?;

                input = &input[32..];

                if input.len() == 20 {
                    // Parse ethereum address in hex
                    let mut buffer = [0; 40];
                    hex::encode_to_slice(input, &mut buffer).unwrap();
                    let recipient_in_hex = str::from_utf8(&buffer).map_err(|_| {
                        ExitError::Other(Cow::from("ERR_INVALID_RECIPIENT_ADDRESS"))
                    })?;
                    // unwrap cannot fail since we checked the length already
                    let recipient_address = Address::try_from_slice(input)
                        .map_err(|_| ExitError::Other(Cow::from("ERR_WRONG_ADDRESS")))?;

                    (
                        nep141_address,
                        // There is no way to inject json, given the encoding of both arguments
                        // as decimal and hexadecimal respectively.
                        format!(
                            r#"{{"amount": "{}", "recipient": "{}"}}"#,
                            amount.as_u128(),
                            recipient_in_hex
                        )
                        .into_bytes(),
                        events::ExitToEth {
                            sender: Address::new(erc20_address),
                            erc20_address: Address::new(erc20_address),
                            dest: recipient_address,
                            amount,
                        },
                    )
                } else {
                    return Err(ExitError::Other(Cow::from("ERR_INVALID_RECIPIENT_ADDRESS")));
                }
            }
            _ => {
                return Err(ExitError::Other(Cow::from(
                    "ERR_INVALID_RECEIVER_ACCOUNT_ID",
                )));
            }
        };

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
        let ethabi::RawLog { topics, data } = exit_event.encode();
        let exit_event_log = Log {
            address: exit_to_ethereum::ADDRESS.raw(),
            topics: topics.into_iter().map(|h| H256::from(h.0)).collect(),
            data,
        };

        Ok(PrecompileOutput {
            logs: vec![promise_log, exit_event_log],
            cost: required_gas,
```

**File:** engine/src/contract_methods/connector.rs (L196-246)
```rust
pub fn exit_to_near_precompile_callback<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<Option<SubmitResult>, ContractError> {
    with_hashchain(io, env, function_name!(), |io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;
        env.assert_private_call()?;

        // This function should only be called as the callback of
        // exactly one promise.
        if handler.promise_results_count() != 1 {
            return Err(errors::ERR_PROMISE_COUNT.into());
        }

        let args: ExitToNearPrecompileCallbackArgs = io.read_input_borsh()?;

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

            None
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

        Ok(maybe_result)
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
