### Title
ExitToEthereum Precompile Schedules Fire-and-Forget Withdrawal Promise With No Error-Recovery Callback, Causing Permanent Fund Loss - (File: engine-precompiles/src/native.rs)

---

### Summary

The `ExitToEthereum` precompile burns user tokens inside the EVM and then schedules a NEAR promise to call `withdraw` on the ETH connector. The promise is created as a bare `PromiseArgs::Create` with no error-handling callback. If the `withdraw` call fails for any reason, the EVM-side burn is already committed and irreversible, permanently destroying the user's funds.

---

### Finding Description

When a user calls the `ExitToEthereum` precompile to bridge ETH or ERC-20 tokens back to Ethereum, the execution flow is:

1. **EVM state is committed**: For ETH exits, the sender's EVM balance is reduced by `context.apparent_value`. For ERC-20 exits, the ERC-20 contract burns the tokens before calling the precompile. Both state changes are committed atomically with the EVM transaction.

2. **A NEAR promise is scheduled**: The precompile constructs a `PromiseCreateArgs` targeting the ETH connector's `withdraw` method and wraps it as `PromiseArgs::Create`.

The critical line is:

```rust
let promise = borsh::to_vec(&PromiseArgs::Create(withdraw_promise)).unwrap();
``` [1](#0-0) 

This promise log is then processed by `filter_promises_from_logs` in the engine, which schedules it as a NEAR promise with no attached callback: [2](#0-1) 

3. **No error callback exists**: Unlike `ExitToNear`, which (when the `error_refund` feature is enabled) wraps the transfer promise in a `PromiseArgs::Callback` that calls `exit_to_near_precompile_callback` to refund tokens on failure, `ExitToEthereum` has **no such mechanism at all** — no feature flag, no callback, no refund path. [3](#0-2) 

The `exit_to_near_precompile_callback` handler that performs refunds on failure is only wired for `ExitToNear`: [4](#0-3) 

`ExitToEthereum` has no equivalent callback registered anywhere in the codebase.

---

### Impact Explanation

**Critical — Permanent freezing/loss of user funds.**

If the `withdraw` call on the ETH connector fails after the EVM transaction has committed:
- ETH exits: the sender's EVM ETH balance has been permanently reduced with no corresponding release on Ethereum.
- ERC-20 exits: the ERC-20 tokens have been permanently burned with no corresponding NEP-141 `withdraw` executed.

In both cases, the funds are destroyed on the Aurora side and never released on the Ethereum side. There is no admin recovery path because the EVM state change is final and no refund callback was ever registered.

---

### Likelihood Explanation

The `withdraw` NEAR promise can fail due to:

- **Insufficient attached gas**: `WITHDRAWAL_GAS` is a fixed constant of `100 TGas`. If the ETH connector's `withdraw` function requires more gas (e.g., due to storage growth or contract upgrades), the promise fails silently. [5](#0-4) 

- **Connector contract issues**: Any panic, assertion failure, or error inside the ETH connector's `withdraw` method causes the promise to fail with no recovery.
- **Chained promise failures**: When multiple exit precompile calls occur in the same EVM transaction, they are chained via `schedule_promise_callback`. A failure in any step leaves subsequent burns unrecovered. [6](#0-5) 

Any unprivileged EVM user can trigger this by calling `ExitToEthereum` and having the downstream NEAR promise fail — a realistic scenario given fixed gas budgets and connector state dependencies.

---

### Recommendation

Mirror the `error_refund` pattern already implemented for `ExitToNear`: wrap the `withdraw_promise` in a `PromiseArgs::Callback` that calls a new `exit_to_ethereum_precompile_callback` method on the engine. That callback should check `promise_result(0)` and, on failure, re-mint the burned tokens (ETH balance or ERC-20) back to the original sender's EVM address. [1](#0-0) 

---

### Proof of Concept

1. User calls `ExitToEthereum` precompile with ETH value (flag `0x00`) targeting a valid Ethereum recipient.
2. Aurora EVM deducts `context.apparent_value` from the sender's EVM balance — committed.
3. Engine schedules `PromiseArgs::Create { method: "withdraw", ... }` on the ETH connector.
4. The `withdraw` call on the ETH connector runs out of gas (100 TGas budget exhausted by connector storage operations).
5. The NEAR promise fails. No callback fires. No refund is issued.
6. User's ETH balance on Aurora is permanently reduced; no ETH is released on Ethereum. [7](#0-6)

### Citations

**File:** engine-precompiles/src/native.rs (L60-62)
```rust
    // TODO(#332): Determine the correct amount of gas
    pub(super) const WITHDRAWAL_GAS: NearGas = NearGas::new(100_000_000_000_000);
}
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

**File:** engine/src/engine.rs (L1648-1665)
```rust
            if log.address == exit_to_near::ADDRESS.raw()
                || log.address == exit_to_ethereum::ADDRESS.raw()
            {
                if log.topics.is_empty() {
                    if let Ok(promise) = PromiseArgs::try_from_slice(&log.data) {
                        match promise {
                            PromiseArgs::Create(promise) => {
                                // Safety: this promise creation is safe because it does not come from
                                // users directly. The exit precompile only create promises which we
                                // are able to execute without violating any security invariants.
                                let id = match previous_promise {
                                    Some(base_id) => {
                                        schedule_promise_callback(handler, base_id, &promise)
                                    }
                                    None => schedule_promise(handler, &promise),
                                };
                                previous_promise = Some(id);
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
