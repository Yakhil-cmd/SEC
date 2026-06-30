### Title
`EvmErc20.withdrawToNear` and `EvmErc20.withdrawToEthereum` Never Check the Precompile Call Return Value, Causing Permanent Token Loss on Failure — (`etc/eth-contracts/contracts/EvmErc20.sol`, `etc/eth-contracts/contracts/EvmErc20V2.sol`)

---

### Summary

In both `EvmErc20.sol` and `EvmErc20V2.sol`, the `withdrawToNear` and `withdrawToEthereum` functions first burn the caller's ERC-20 tokens via `_burn`, then invoke the exit precompile via inline assembly. The return value of the `call` opcode — stored in the local assembly variable `res` — is **never checked**. If the precompile call fails for any reason, the tokens are permanently destroyed with no transfer to the destination chain and no revert.

---

### Finding Description

In `EvmErc20.sol`:

```solidity
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);                          // tokens destroyed here

    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    uint input_size = 1 + 32 + recipient.length;

    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        // res is assigned but NEVER checked
    }
}
``` [1](#0-0) 

The identical pattern appears in `withdrawToEthereum` (flag `0x01`, precompile `0xb0bd02f6...`): [2](#0-1) 

And in `EvmErc20V2.sol` for both withdrawal directions: [3](#0-2) [4](#0-3) 

The `ExitToNear` precompile (`engine-precompiles/src/native.rs`) can legitimately fail and return an `ExitError` for several reasons reachable by an unprivileged caller: invalid recipient account ID format, input size out of range, or the ERC-20 address not being registered in the NEP-141 map. When the precompile fails, the EVM `call` opcode returns `0` into `res`. Because `res` is never inspected, the outer Solidity function does **not** revert — it returns successfully. The `_burn` that already executed is not rolled back. [5](#0-4) [6](#0-5) 

The `ExitToNear` precompile validates the recipient with `parse_recipient`, which calls `AccountId::try_from` and returns `ExitError::Other("ERR_INVALID_RECEIVER_ACCOUNT_ID")` on failure. This error causes the `call` opcode to return `0`, but the Solidity wrapper never observes it. [6](#0-5) 

---

### Impact Explanation

**Critical — Permanent freezing of funds.**

When the precompile call fails silently:
- The ERC-20 tokens are already burned (supply reduced, user balance zeroed).
- No NEP-141 tokens are transferred on the NEAR side.
- No revert occurs, so the transaction is recorded as successful.
- There is no recovery path: the burned tokens cannot be re-minted and the NEAR-side transfer never happened.

The `error_refund` feature in `EvmErc20V2` (which embeds the sender address for a callback refund) only handles failures of the *downstream NEAR-side* `ft_transfer` call — it does not protect against the precompile call itself returning `0` at the EVM level.

---

### Likelihood Explanation

**Medium.** Any unprivileged ERC-20 token holder can trigger this by calling `withdrawToNear` with a recipient string that fails NEAR account ID validation (e.g., uppercase letters, invalid characters, or a string exceeding the 64-byte NEAR account ID limit). The `recipient` parameter is a raw `bytes` argument with no Solidity-level validation before the burn. A user making a typo in the recipient account ID would permanently lose their tokens.

---

### Recommendation

Check `res` after the assembly `call` and revert if it is zero:

```solidity
assembly {
    let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
    if iszero(res) { revert(0, 0) }
}
```

Apply the same fix to `withdrawToEthereum` in both `EvmErc20.sol` and `EvmErc20V2.sol`. Alternatively, restructure so that `_burn` is only called after confirming the precompile call succeeded, or use a checks-effects-interactions pattern with a re-mint on failure.

---

### Proof of Concept

1. Deploy `EvmErc20` (or `EvmErc20V2`) with a valid NEP-141 backing token.
2. Mint tokens to `alice`.
3. `alice` calls `withdrawToNear(bytes("INVALID ACCOUNT ID WITH SPACES"), amount)`.
4. `_burn(alice, amount)` executes — alice's balance drops to zero.
5. The precompile call fails (invalid account ID), `call` returns `0` into `res`.
6. `res` is never checked; the function returns without reverting.
7. The transaction is mined successfully. Alice's tokens are gone. No NEP-141 transfer occurred on NEAR.

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

**File:** etc/eth-contracts/contracts/EvmErc20.sol (L65-76)
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

**File:** etc/eth-contracts/contracts/EvmErc20V2.sol (L66-77)
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
```

**File:** engine-precompiles/src/native.rs (L295-309)
```rust
fn validate_input_size(input: &[u8], min: usize, max: usize) -> Result<(), ExitError> {
    if input.len() < min || input.len() > max {
        return Err(ExitError::Other(Cow::from("ERR_INVALID_INPUT")));
    }
    Ok(())
}

fn get_nep141_from_erc20<I: IO>(erc20_token: &[u8], io: &I) -> Result<AccountId, ExitError> {
    AccountId::try_from(
        io.read_storage(bytes_to_key(KeyPrefix::Erc20Nep141Map, erc20_token).as_slice())
            .map(|s| s.to_vec())
            .ok_or(ExitError::Other(Cow::Borrowed(ERR_TARGET_TOKEN_NOT_FOUND)))?,
    )
    .map_err(|_| ExitError::Other(Cow::Borrowed("ERR_INVALID_NEP141_ACCOUNT")))
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
