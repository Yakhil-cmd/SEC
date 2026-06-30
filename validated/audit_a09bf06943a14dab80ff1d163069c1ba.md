### Title
Unchecked Exit-Precompile Return Value in `withdrawToNear`/`withdrawToEthereum` Causes Permanent ERC-20 Token Loss - (File: `etc/eth-contracts/contracts/EvmErc20.sol`, `etc/eth-contracts/contracts/EvmErc20V2.sol`)

### Summary

Both `EvmErc20.sol` and `EvmErc20V2.sol` burn the caller's ERC-20 tokens before calling the `ExitToNear` precompile via inline assembly. The return value of that `call` opcode is stored in a local variable `res` but is never checked. If the precompile returns failure (0), the burn is already committed and irreversible, but no NEAR-side `ft_transfer` promise is ever scheduled. The user's bridged tokens are permanently destroyed with no corresponding NEAR transfer.

### Finding Description

In `EvmErc20.sol` and `EvmErc20V2.sol`, both `withdrawToNear` and `withdrawToEthereum` follow the same pattern:

```solidity
// EvmErc20.sol lines 53-63
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);          // <-- tokens destroyed here

    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    uint input_size = 1 + 32 + recipient.length;

    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        // res is NEVER checked
    }
}
``` [1](#0-0) [2](#0-1) 

The `ExitToNear` precompile (`engine-precompiles/src/native.rs`) can return a hard failure (`ExitError`) in several user-reachable paths:

1. **Invalid NEAR account ID in `recipient`**: `parse_recipient` validates the bytes as a NEAR account ID. Any byte sequence that is not valid UTF-8, contains uppercase letters, starts/ends with a separator, or exceeds 64 characters causes `ERR_INVALID_RECEIVER_ACCOUNT_ID`. [3](#0-2) 

2. **Amount exceeds `u128::MAX`**: `parse_amount` returns `ERR_INVALID_AMOUNT`. [4](#0-3) 

3. **ERC-20 not registered**: `get_nep141_from_erc20` returns `ERR_TARGET_TOKEN_NOT_FOUND` if the NEP-141 mapping is absent. [5](#0-4) 

When the precompile returns an error, the EVM `call` opcode returns `0` into `res`. Because `res` is never inspected, the Solidity function returns normally. The `_burn` that already executed is not rolled back. No promise log is emitted, so `filter_promises_from_logs` schedules nothing on the NEAR side. [6](#0-5) 

The `error_refund` feature only handles the case where the NEAR-side `ft_transfer` promise fails *after* the precompile succeeds. It provides no protection when the precompile call itself fails before any promise is created. [7](#0-6) 

### Impact Explanation

**Permanent freezing/loss of funds.** The ERC-20 tokens are burned (supply reduced, balance zeroed) but no corresponding NEP-141 tokens are released on NEAR. The user loses the full `amount` with no recovery path. This matches the "Permanent freezing of funds" impact class.

### Likelihood Explanation

The `recipient` parameter of `withdrawToNear` is a raw `bytes` argument with no Solidity-level validation. A user who passes any of the following will silently lose funds:
- A recipient string with uppercase letters (e.g., `"Alice.near"`)
- A recipient starting or ending with `-`, `_`, or `.` (e.g., `"-alice.near"`)
- A recipient longer than 64 characters
- Any non-UTF-8 byte sequence

These are common mistakes for users unfamiliar with NEAR account ID rules. The transaction succeeds from the EVM perspective (no revert, no error event), giving no indication that the withdrawal failed.

### Recommendation

Add a `require(res != 0, "ERR_EXIT_PRECOMPILE_FAILED")` check immediately after the assembly block in both `withdrawToNear` and `withdrawToEthereum` in both `EvmErc20.sol` and `EvmErc20V2.sol`. This causes the entire transaction to revert if the precompile fails, rolling back the `_burn` and preserving the user's balance.

```solidity
assembly {
    let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
    if iszero(res) { revert(0, 0) }
}
```

### Proof of Concept

1. Deploy a bridged ERC-20 token (registered NEP-141 ↔ ERC-20 mapping exists).
2. Mint tokens to address `A`.
3. From address `A`, call `withdrawToNear(bytes("-invalid.near"), amount)`.
   - `_burn(A, amount)` executes; `A`'s balance drops to 0.
   - The precompile call is made with `recipient = "-invalid.near"`.
   - `parse_recipient` in `engine-precompiles/src/native.rs` calls `"-invalid.near".parse::<AccountId>()`, which fails with `ERR_INVALID_RECEIVER_ACCOUNT_ID` because the account ID starts with `-`.
   - The precompile returns `ExitError`, the `call` opcode returns `res = 0`.
   - `res` is never checked; the Solidity function returns normally.
4. Observe: `A`'s ERC-20 balance is 0, no NEP-141 tokens were transferred to any NEAR account. Funds are permanently lost. [1](#0-0) [3](#0-2) [8](#0-7)

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

**File:** etc/eth-contracts/contracts/EvmErc20V2.sol (L53-64)
```text
    function withdrawToNear(bytes memory recipient, uint256 amount) external override {
        address sender = _msgSender();
        _burn(sender, amount);

        bytes32 amount_b = bytes32(amount);
        bytes memory input = abi.encodePacked("\x01", sender, amount_b, recipient);
        uint input_size = 1 + 20 + 32 + recipient.length;

        assembly {
            let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        }
    }
```

**File:** engine-precompiles/src/native.rs (L302-309)
```rust
fn get_nep141_from_erc20<I: IO>(erc20_token: &[u8], io: &I) -> Result<AccountId, ExitError> {
    AccountId::try_from(
        io.read_storage(bytes_to_key(KeyPrefix::Erc20Nep141Map, erc20_token).as_slice())
            .map(|s| s.to_vec())
            .ok_or(ExitError::Other(Cow::Borrowed(ERR_TARGET_TOKEN_NOT_FOUND)))?,
    )
    .map_err(|_| ExitError::Other(Cow::Borrowed("ERR_INVALID_NEP141_ACCOUNT")))
}
```

**File:** engine-precompiles/src/native.rs (L337-344)
```rust
fn parse_amount(input: &[u8]) -> Result<U256, ExitError> {
    let amount = U256::from_big_endian(input);

    if amount > U256::from(u128::MAX) {
        return Err(ExitError::Other(Cow::from("ERR_INVALID_AMOUNT")));
    }

    Ok(amount)
```

**File:** engine-precompiles/src/native.rs (L359-378)
```rust
fn parse_recipient(recipient: &[u8]) -> Result<Recipient<'_>, ExitError> {
    let recipient = str::from_utf8(recipient)
        .map_err(|_| ExitError::Other(Cow::from("ERR_INVALID_RECEIVER_ACCOUNT_ID")))?;
    let (receiver_account_id, message) = recipient.split_once(':').map_or_else(
        || (recipient, None),
        |(recipient, msg)| {
            if msg == UNWRAP_WNEAR_MSG {
                (recipient, Some(Message::UnwrapWnear))
            } else {
                (recipient, Some(Message::Omni(msg)))
            }
        },
    );

    Ok(Recipient {
        receiver_account_id: receiver_account_id
            .parse()
            .map_err(|_| ExitError::Other(Cow::from("ERR_INVALID_RECEIVER_ACCOUNT_ID")))?,
        message,
    })
```

**File:** engine-precompiles/src/native.rs (L449-483)
```rust
        let callback_args = ExitToNearPrecompileCallbackArgs {
            #[cfg(feature = "error_refund")]
            refund: refund_call_args(&exit_to_near_params, &exit_event),
            #[cfg(not(feature = "error_refund"))]
            refund: None,
            transfer_near: transfer_near_args,
        };
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

**File:** engine-precompiles/src/native.rs (L727-775)
```rust
impl<'a> TryFrom<&'a [u8]> for ExitToNearParams<'a> {
    type Error = ExitError;

    fn try_from(input: &'a [u8]) -> Result<Self, Self::Error> {
        // The first byte of the input is a flag, selecting the behavior to be triggered:
        // 0x00 -> Eth(base) token withdrawal
        // 0x01 -> ERC-20 token withdrawal
        let flag = input
            .first()
            .copied()
            .ok_or_else(|| ExitError::Other(Cow::from("ERR_MISSING_FLAG")))?;

        #[cfg(feature = "error_refund")]
        let (refund_address, input) = parse_input(input)?;
        #[cfg(not(feature = "error_refund"))]
        let input = parse_input(input)?;

        match flag {
            0x0 => {
                let Recipient {
                    receiver_account_id,
                    message,
                } = parse_recipient(input)?;

                Ok(Self::BaseToken(BaseTokenParams {
                    #[cfg(feature = "error_refund")]
                    refund_address,
                    receiver_account_id,
                    message,
                }))
            }
            0x1 => {
                let amount = parse_amount(&input[..32])?;
                let Recipient {
                    receiver_account_id,
                    message,
                } = parse_recipient(&input[32..])?;

                Ok(Self::Erc20TokenParams(Erc20TokenParams {
                    #[cfg(feature = "error_refund")]
                    refund_address,
                    receiver_account_id,
                    amount,
                    message,
                }))
            }
            _ => Err(ExitError::Other(Cow::from("ERR_INVALID_FLAG"))),
        }
    }
```

**File:** engine/src/engine.rs (L1634-1685)
```rust
fn filter_promises_from_logs<I, T, P>(
    io: &I,
    handler: &mut P,
    logs: T,
    current_account_id: &AccountId,
) -> Vec<ResultLog>
where
    T: IntoIterator<Item = Log>,
    P: PromiseHandler,
    I: IO + Copy,
{
    let mut previous_promise: Option<PromiseId> = None;
    logs.into_iter()
        .filter_map(|log| {
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
                            PromiseArgs::Callback(promise) => {
                                // Safety: This is safe because the promise data comes from our own
                                // exit precompiles. See note above.
                                let base_id = match previous_promise {
                                    Some(base_id) => {
                                        schedule_promise_callback(handler, base_id, &promise.base)
                                    }
                                    None => schedule_promise(handler, &promise.base),
                                };
                                let id =
                                    schedule_promise_callback(handler, base_id, &promise.callback);
                                previous_promise = Some(id);
                            }
                            PromiseArgs::Recursive(_) => {
                                unreachable!("Exit precompiles do not produce recursive promises")
                            }
                        }
                    }
                    // do not pass on these "internal logs" to the caller
                    None
```
