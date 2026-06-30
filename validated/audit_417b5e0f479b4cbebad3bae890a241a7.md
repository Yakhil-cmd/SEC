### Title
No-Callback Promise in `ExitToEthereum` Precompile Causes Permanent Fund Freeze on Withdrawal Failure - (File: `engine-precompiles/src/native.rs`)

### Summary
The `ExitToEthereum` precompile always schedules its NEAR-side `withdraw` promise as a bare `PromiseArgs::Create` with no failure callback. If the downstream `withdraw` call on the eth-connector contract fails for any reason, the ETH (or burned ERC-20 tokens) that were already deducted from the user's EVM balance are permanently unrecoverable. This is the Aurora-Engine analog of the Solidity `transfer()` pattern: a fund-transfer mechanism that silently fails with no recovery path.

### Finding Description

When a user calls the `ExitToEthereum` precompile (`0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab`) to bridge ETH or ERC-20 tokens back to Ethereum, the precompile's `run()` function always constructs a bare `PromiseArgs::Create` with no attached callback:

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

There is no `PromiseArgs::Callback` wrapping this promise, and no `exit_to_ethereum_precompile_callback` equivalent exists anywhere in the codebase. The EVM state changes (ETH deducted from caller, credited to precompile address; or ERC-20 tokens burned) are committed before the NEAR promise is dispatched. If the `withdraw` promise fails, those state changes are not rolled back.

Contrast this with `ExitToNear`, which wraps its transfer promise in a `PromiseArgs::Callback` pointing to `exit_to_near_precompile_callback`, which calls `engine::refund_on_error` to restore balances on failure:

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

The `exit_to_near_precompile_callback` handler explicitly handles the failure branch:

```rust
} else if let Some(args) = args.refund {
    // Exit call failed; need to refund tokens
    let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;
``` [3](#0-2) 

No such handler exists for `ExitToEthereum`. The `refund_on_error` function itself confirms the two refund paths (ETH and ERC-20), but it is only reachable from the `ExitToNear` callback: [4](#0-3) 

The codebase's own test suite acknowledges this asymmetry explicitly:

```rust
// If the refund feature is not enabled, then there is no refund in the EVM
#[cfg(not(feature = "error_refund"))]
let expected_balance = Wei::new_u64(INITIAL_ETH_BALANCE - ETH_EXIT_AMOUNT);
``` [5](#0-4) 

But even with `error_refund` enabled, `ExitToEthereum` has no callback at all — the feature flag only affects `ExitToNear`.

### Impact Explanation

**ETH exit (flag `0x00`):** The caller sends ETH value to the precompile. The EVM transfers that ETH from the caller's account to the precompile address `0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab`. If the NEAR-side `withdraw` promise fails, the ETH is permanently stranded at the precompile address with no recovery mechanism. No function in the contract can sweep or return it.

**ERC-20 exit (flag `0x01`):** The ERC-20 contract burns the user's tokens before calling the precompile. If the NEAR-side `withdraw` promise fails, the tokens are permanently destroyed with no re-mint path.

Both cases result in **permanent freezing/destruction of user funds**.

### Likelihood Explanation

The NEAR-side `withdraw` call on the eth-connector can fail under realistic conditions:

1. The eth-connector contract is paused for maintenance or emergency — a known operational mode.
2. `costs::WITHDRAWAL_GAS` is insufficient for the connector's current logic (gas costs on NEAR can change with protocol upgrades).
3. The eth-connector account ID stored in Aurora's state has been rotated but the old account no longer accepts `withdraw` calls.

Any EVM user who calls `ExitToEthereum` during such a window loses their funds permanently. The user has no way to predict or prevent this at call time.

### Recommendation

Add a failure-handling callback to `ExitToEthereum` analogous to `exit_to_near_precompile_callback`. The promise should be wrapped as `PromiseArgs::Callback` with a new `exit_to_ethereum_precompile_callback` method that:

- For ETH exit: transfers ETH back from the precompile address to the original caller (mirroring the `refund_on_error` ETH branch).
- For ERC-20 exit: re-mints the burned tokens to the original caller (mirroring the `refund_on_error` ERC-20 branch).

The `RefundCallArgs` struct already supports both cases: [6](#0-5) 

### Proof of Concept

1. User holds 1 ETH on Aurora EVM.
2. User calls `ExitToEthereum` precompile at `0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab` with `flag=0x00` and a valid Ethereum recipient, attaching 1 ETH.
3. EVM deducts 1 ETH from user, credits it to the precompile address. A NEAR promise to call `withdraw` on the eth-connector is scheduled.
4. The eth-connector contract is paused (or `WITHDRAWAL_GAS` is exhausted), causing the `withdraw` promise to fail.
5. No callback fires. The 1 ETH remains at `0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab` permanently.
6. The user's EVM balance is 0. The precompile address holds 1 ETH with no extraction path. Funds are permanently frozen. [7](#0-6)

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

**File:** engine-precompiles/src/native.rs (L844-1003)
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
            output: Vec::new(),
        })
    }
```

**File:** engine/src/contract_methods/connector.rs (L231-237)
```rust
        } else if let Some(args) = args.refund {
            // Exit call failed; need to refund tokens
            let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;

            if !refund_result.status.is_ok() {
                return Err(errors::ERR_REFUND_FAILURE.into());
            }
```

**File:** engine/src/engine.rs (L1176-1224)
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
```

**File:** engine-tests/src/tests/erc20_connector.rs (L773-775)
```rust
        // If the refund feature is not enabled, then there is no refund in the EVM
        #[cfg(not(feature = "error_refund"))]
        let expected_balance = Wei::new_u64(INITIAL_ETH_BALANCE - ETH_EXIT_AMOUNT);
```

**File:** engine-types/src/parameters/connector.rs (L115-120)
```rust
#[derive(Debug, Clone, BorshSerialize, BorshDeserialize, PartialEq, Eq)]
pub struct RefundCallArgs {
    pub recipient_address: Address,
    pub erc20_address: Option<Address>,
    pub amount: RawU256,
}
```
