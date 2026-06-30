### Title
Unchecked Precompile Return Value After `_burn` in `EvmErc20.withdrawToNear` / `withdrawToEthereum` Causes Permanent Token Loss — (File: `etc/eth-contracts/contracts/EvmErc20.sol`)

---

### Summary

`EvmErc20.sol` and `EvmErc20V2.sol` burn a user's ERC-20 tokens **before** calling the `ExitToNear` or `ExitToEthereum` precompile, but never check the assembly `call` return value. If the precompile rejects the call for any reason, the tokens are permanently destroyed with no NEAR-side transfer and no refund — a permanent fund freeze reachable by any EVM user.

---

### Finding Description

In `etc/eth-contracts/contracts/EvmErc20.sol`, both withdrawal functions follow the same pattern:

```solidity
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);          // ← tokens destroyed here

    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    uint input_size = 1 + 32 + recipient.length;

    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f,
                        0, add(input, 32), input_size, 0, 32)
        // res is NEVER checked
    }
}
``` [1](#0-0) 

The same pattern exists in `withdrawToEthereum` and in `EvmErc20V2.sol`: [2](#0-1) [3](#0-2) 

The `ExitToNear` precompile (`engine-precompiles/src/native.rs`) can return failure (`ExitError`) in several reachable ways:

1. **Invalid UTF-8 recipient bytes** — `parse_recipient` calls `str::from_utf8(recipient)` and returns `ERR_INVALID_RECEIVER_ACCOUNT_ID` on failure.
2. **Amount exceeds `u128::MAX`** — `parse_amount` returns `ERR_INVALID_AMOUNT` for any `U256 > u128::MAX`.
3. **ERC-20 not in NEP-141 map** — `get_nep141_from_erc20` returns `ERR_TARGET_TOKEN_NOT_FOUND`. [4](#0-3) [5](#0-4) 

When the precompile returns failure, the EVM `call` opcode sets `res = 0`. Because the Solidity code never inspects `res`, the outer transaction **succeeds** from the EVM perspective. The `_burn` is not reverted. The tokens are gone.

---

### Impact Explanation

**Critical — Permanent freezing of funds.**

Any ERC-20 tokens bridged via the Aurora connector (i.e., every `EvmErc20`/`EvmErc20V2` instance) can be permanently destroyed without a corresponding NEAR-side credit. The user loses the full `amount` with no recovery path, because:

- The EVM state is committed (burn is final).
- No NEAR-side promise is ever scheduled (the precompile never emitted a log).
- There is no refund callback triggered (the `error_refund` feature only fires when the precompile *succeeds* but the downstream NEAR call fails). [6](#0-5) 

---

### Likelihood Explanation

**Medium.**

- The `recipient` parameter is `bytes memory` — Solidity accepts any byte sequence. A user passing a non-UTF-8 byte sequence (e.g., `0xFF`) triggers the failure path immediately.
- A user holding a balance representable as `uint256` but exceeding `u128::MAX` (possible for tokens with large supplies) will hit `ERR_INVALID_AMOUNT`.
- Third-party integrators calling `withdrawToNear` programmatically with raw bytes are a realistic trigger.
- No special privilege is required; any token holder can trigger this.

---

### Recommendation

Check the return value of the precompile `call` and revert on failure in both functions and both contracts:

```solidity
assembly {
    let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f,
                    0, add(input, 32), input_size, 0, 32)
    if iszero(res) { revert(0, 0) }
}
```

Alternatively, restructure the functions to call the precompile **before** burning, and only burn if the precompile call succeeds.

Apply the same fix to `withdrawToEthereum` in both `EvmErc20.sol` and `EvmErc20V2.sol`.

---

### Proof of Concept

1. Deploy an `EvmErc20` token mapped to a registered NEP-141.
2. Mint 1000 tokens to `alice`.
3. `alice` calls `withdrawToNear(bytes(hex"ff"), 1000)` — `0xff` is not valid UTF-8.
4. `_burn(alice, 1000)` executes; alice's balance drops to 0.
5. The precompile call returns 0 (`ERR_INVALID_RECEIVER_ACCOUNT_ID`).
6. `res` is never checked; the transaction reverts nothing and succeeds.
7. Alice's 1000 tokens are permanently destroyed. No NEAR-side transfer occurs. [1](#0-0) [7](#0-6)

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

**File:** etc/eth-contracts/contracts/EvmErc20.sol (L65-77)
```text
    function withdrawToEthereum(address recipient, uint256 amount) external override {
        _burn(_msgSender(), amount);

        bytes32 amount_b = bytes32(amount);
        bytes20 recipient_b = bytes20(recipient);
        bytes memory input = abi.encodePacked("\x01", amount_b, recipient_b);
        uint input_size = 1 + 32 + 20;

        assembly {
            let res := call(gas(), 0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab, 0, add(input, 32), input_size, 0, 32)
        }
    }
}
```

**File:** etc/eth-contracts/contracts/EvmErc20V2.sol (L53-63)
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

**File:** engine-precompiles/src/native.rs (L337-345)
```rust
fn parse_amount(input: &[u8]) -> Result<U256, ExitError> {
    let amount = U256::from_big_endian(input);

    if amount > U256::from(u128::MAX) {
        return Err(ExitError::Other(Cow::from("ERR_INVALID_AMOUNT")));
    }

    Ok(amount)
}
```

**File:** engine-precompiles/src/native.rs (L359-379)
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
}
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
