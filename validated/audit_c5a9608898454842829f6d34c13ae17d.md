### Title
Silent Precompile Failure After Token Burn Causes Permanent Fund Loss — (`etc/eth-contracts/contracts/EvmErc20.sol`)

---

### Summary

In `EvmErc20.sol` and `EvmErc20V2.sol`, the `withdrawToNear()` and `withdrawToEthereum()` functions burn the caller's ERC-20 tokens **before** invoking the exit precompile via inline assembly. The assembly `call` return value is captured in `res` but **never checked**. If the precompile call fails for any reachable reason, the ERC-20 tokens are permanently destroyed with no corresponding credit on the NEAR side and no revert, causing irreversible fund loss.

---

### Finding Description

`EvmErc20.sol::withdrawToNear()` executes the following sequence:

```solidity
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);                          // (1) tokens destroyed

    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    uint input_size = 1 + 32 + recipient.length;

    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f,
                        0, add(input, 32), input_size, 0, 32)
        // (2) res is NEVER checked — silent failure is possible
    }
}
```

The `ExitToNear` precompile (`engine-precompiles/src/native.rs`) can return failure (EVM `call` returns 0) for several reachable, user-controlled conditions:

1. **Invalid NEAR account ID as recipient** — `parse_recipient` calls `receiver_account_id.parse()` and returns `ERR_INVALID_RECEIVER_ACCOUNT_ID` on failure. An empty byte string, a string with illegal characters, or a string that is too long all trigger this path.
2. **Amount exceeds `u128::MAX`** — `parse_amount` explicitly rejects values above `u128::MAX`. Because `amount` is a Solidity `uint256`, values above this threshold are valid on the EVM side but rejected by the precompile.
3. **ERC-20 not registered in the NEP-141 map** — `get_nep141_from_erc20` returns `ERR_TARGET_TOKEN_NOT_FOUND` if the mapping key is absent.

In all three cases the Solidity function has already executed `_burn()` successfully. Because `res` is never inspected, the transaction does **not** revert. The caller loses their tokens with no on-chain indication of failure and no refund path.

`EvmErc20V2.sol::withdrawToNear()` and both contracts' `withdrawToEthereum()` share the identical unchecked-assembly pattern.

---

### Impact Explanation

**Critical — Permanent freezing / destruction of funds.**

A user's ERC-20 tokens are irreversibly burned. No corresponding NEP-141 balance is credited. There is no recovery mechanism: the tokens cannot be re-minted (mint is `onlyAdmin`), and the NEAR-side balance was never incremented. The loss is permanent and proportional to the withdrawn amount.

---

### Likelihood Explanation

**Medium.** The three triggering conditions are all reachable by an ordinary, unprivileged EVM user:

- Passing an empty or malformed recipient byte string is a natural user mistake.
- Any ERC-20 whose admin minted a balance exceeding `u128::MAX` (possible with a `uint256` mint call) will silently fail on any withdrawal attempt.
- A freshly deployed ERC-20 whose NEP-141 mapping has not yet been registered will silently fail on every withdrawal.

No special privilege, governance capture, or external oracle is required.

---

### Recommendation

Check the assembly `call` return value and revert on failure in both `withdrawToNear` and `withdrawToEthereum` in both `EvmErc20.sol` and `EvmErc20V2.sol`:

```solidity
assembly {
    let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f,
                    0, add(input, 32), input_size, 0, 32)
    if iszero(res) { revert(0, 0) }
}
```

This ensures the burn is atomically rolled back whenever the precompile rejects the call, preserving the ERC-20 ↔ NEP-141 accounting invariant.

---

### Proof of Concept

1. Admin deploys `EvmErc20` and mints `1000` tokens to Alice.
2. Alice calls `withdrawToNear(bytes(""), 1000)` — an empty recipient.
3. `_burn(alice, 1000)` executes successfully; Alice's ERC-20 balance drops to 0.
4. The precompile receives flag `0x01`, parses the 32-byte amount, then calls `parse_recipient` on an empty slice. `"".parse::<AccountId>()` fails → precompile returns `ExitError` → EVM `call` returns `0`.
5. `res = 0` is stored but never checked; the Solidity function returns without reverting.
6. Alice's 1000 ERC-20 tokens are permanently destroyed. No NEP-141 tokens are transferred. No refund is possible.

**Affected files:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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
