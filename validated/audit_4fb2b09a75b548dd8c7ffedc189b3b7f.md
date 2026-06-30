### Title
Unchecked Precompile Call Return Value in `withdrawToNear`/`withdrawToEthereum` Causes Permanent Token Burn Without NEAR-Side Transfer — (`File: etc/eth-contracts/contracts/EvmErc20.sol`)

---

### Summary

Both `EvmErc20.sol` and `EvmErc20V2.sol` burn ERC-20 tokens **before** calling the `ExitToNear` / `ExitToEthereum` precompile, and the assembly `call` return value is never checked. If the precompile call fails for any reason, the EVM-side burn is committed while no NEAR-side NEP-141 transfer is ever scheduled, permanently destroying the user's bridged tokens.

---

### Finding Description

In `EvmErc20.sol` the `withdrawToNear` function is:

```solidity
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);          // ← burn is irreversible

    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    uint input_size = 1 + 32 + recipient.length;

    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f,
                        0, add(input, 32), input_size, 0, 32)
        // res is never checked — no revert on failure
    }
}
``` [1](#0-0) 

The identical pattern appears in `withdrawToEthereum` (same file, lines 65-76) and in `EvmErc20V2.sol`: [2](#0-1) [3](#0-2) 

The `ExitToNear` precompile (`engine-precompiles/src/native.rs`) can return `ExitError` — causing the EVM `call` to return `res = 0` — in several reachable paths:

1. **Invalid recipient bytes** — `parse_recipient` returns `ERR_INVALID_RECEIVER_ACCOUNT_ID` if the `recipient` bytes are not valid UTF-8 or do not form a valid NEAR account ID.
2. **ERC-20 not registered** — `get_nep141_from_erc20` returns `ERR_TARGET_TOKEN_NOT_FOUND` if the calling ERC-20 address has no entry in the `Erc20Nep141Map`.
3. **Eth-connector key absent** — `get_eth_connector_contract_account` returns `ERR_KEY_NOT_FOUND`. [4](#0-3) [5](#0-4) 

Because the Solidity code never inspects `res`, the outer function returns successfully in all failure cases. The `_burn` that already executed is not rolled back, so the tokens are destroyed on the EVM side with no corresponding NEAR-side `ft_transfer` promise ever created.

The accounting mismatch is structurally identical to the Balancer batchSwap bug: a multi-step bridge operation (burn EVM tokens → transfer NEP-141 tokens) only commits the first step while silently skipping the second, leaving the bridge's cross-chain accounting permanently inconsistent.

---

### Impact Explanation

**Critical — Permanent freezing of funds.**

A user's ERC-20 tokens (which represent locked NEP-141 tokens held by the eth-connector) are irreversibly burned on the EVM side. Because no NEAR promise is created, the corresponding NEP-141 tokens remain locked in the eth-connector contract forever with no mechanism to recover them. The user loses the full `amount` with no recourse.

---

### Likelihood Explanation

**Low-Medium.** The trigger condition is reachable by any unprivileged EVM user:

- Passing a `recipient` containing bytes that are not a valid NEAR account ID (e.g., uppercase letters, special characters, or raw binary data) is sufficient to cause `parse_recipient` to return an error and the precompile to fail.
- A buggy integration, a direct low-level call, or a user copy-pasting a malformed account string are all realistic paths.
- No admin compromise or special privilege is required.

---

### Recommendation

Revert the entire transaction if the precompile call fails. Replace the unchecked assembly block with one that checks `res`:

```solidity
assembly {
    let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f,
                    0, add(input, 32), input_size, 0, 32)
    if iszero(res) { revert(0, 0) }
}
```

Alternatively, restructure the function so the precompile is called **before** `_burn`, and only burn if the precompile call succeeds. This makes the operation atomic and prevents the irreversible burn from occurring when the NEAR-side transfer cannot be scheduled.

Apply the same fix to `withdrawToEthereum` in both `EvmErc20.sol` and `EvmErc20V2.sol`.

---

### Proof of Concept

1. A user holds 1000 units of a bridged ERC-20 token (e.g., `USDC.e`) deployed as `EvmErc20`.
2. The user calls `withdrawToNear(bytes("\xFF\xFE"), 1000)` — `\xFF\xFE` is not valid UTF-8.
3. `_burn(msg.sender, 1000)` executes and commits: the user's EVM balance drops to 0.
4. The precompile call is made. Inside `parse_recipient`, `str::from_utf8(b"\xFF\xFE")` fails, returning `ExitError::Other("ERR_INVALID_RECEIVER_ACCOUNT_ID")`. The EVM `call` returns `res = 0`.
5. The assembly block does not check `res`; the Solidity function returns normally.
6. No NEAR promise is created. The NEP-141 tokens remain locked in the eth-connector.
7. The user has lost 1000 tokens permanently: burned on EVM, unreachable on NEAR. [6](#0-5) [1](#0-0)

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
