### Title
Unchecked Exit Precompile Return Value in `withdrawToNear`/`withdrawToEthereum` Causes Permanent Token Burn Without Bridge Transfer - (File: `etc/eth-contracts/contracts/EvmErc20.sol`, `etc/eth-contracts/contracts/EvmErc20V2.sol`)

### Summary
Both `EvmErc20.sol` and `EvmErc20V2.sol` burn the caller's ERC-20 tokens before calling the Aurora exit precompile via inline assembly, but never check whether the precompile call succeeded. If the precompile call fails for any reason, the tokens are permanently destroyed with no corresponding NEAR or Ethereum transfer, resulting in permanent loss of user funds.

### Finding Description
In `EvmErc20.sol`, the `withdrawToNear` function executes the following sequence:

1. Burns the caller's tokens unconditionally via `_burn(_msgSender(), amount)`.
2. Encodes the call input and invokes the `ExitToNear` precompile at `0xe9217bc70b7ed1f598ddd3199e80b093fa71124f` via inline assembly.
3. Captures the return value `res` but **never checks it**.

```solidity
// EvmErc20.sol lines 53-63
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

The identical pattern exists in `EvmErc20V2.sol`:

```solidity
// EvmErc20V2.sol lines 53-64
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

And in `withdrawToEthereum` in both contracts:

```solidity
// EvmErc20.sol lines 65-76
function withdrawToEthereum(address recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);
    ...
    assembly {
        let res := call(gas(), 0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab, 0, add(input, 32), input_size, 0, 32)
    }
}
```

The `ExitToNear` precompile (`engine-precompiles/src/native.rs`) can fail with multiple error conditions: `ERR_INVALID_RECEIVER_ACCOUNT_ID`, `ERR_TARGET_TOKEN_NOT_FOUND`, `ERR_INVALID_AMOUNT`, `ERR_ETH_ATTACHED_FOR_ERC20_EXIT`, `ERR_INVALID_INPUT`, and others. When the precompile returns failure (EVM `call` returns 0), Solidity does not automatically revert the outer call. Because `_burn` already executed and the assembly block does not revert on failure, the tokens are permanently destroyed.

This is the direct analog of the reported vulnerability class: a critical token operation (here, the bridge exit call) whose return value is silently ignored, causing irreversible state corruption when the operation fails.

### Impact Explanation
**Critical — Permanent freezing/loss of user funds.**

When the precompile call fails and `res == 0`, the ERC-20 tokens are already burned from the user's balance. No NEAR (or ETH) is transferred. The tokens are gone with no recovery path. This constitutes permanent destruction of user funds.

### Likelihood Explanation
**Medium.** Any unprivileged token holder can trigger this by calling `withdrawToNear` with:
- A `recipient` byte string that is not a valid NEAR account ID (e.g., too long, invalid characters). The precompile's `parse_recipient` will return `ERR_INVALID_RECEIVER_ACCOUNT_ID`, the precompile call returns 0, but the burn is not reverted.
- A token that is not registered in the NEP-141↔ERC-20 map, causing `ERR_TARGET_TOKEN_NOT_FOUND`.
- Insufficient gas forwarded to the precompile.

No admin access or special privileges are required. The attacker-controlled entry path is a direct call to `withdrawToNear(bytes recipient, uint256 amount)` on any deployed `EvmErc20` or `EvmErc20V2` contract.

### Recommendation
Check the return value of the assembly `call` and revert if it is zero:

```solidity
assembly {
    let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
    if iszero(res) { revert(0, 0) }
}
```

Alternatively, restructure the function to call the precompile **before** burning, and only burn if the precompile call succeeds. This mirrors the `safeApprove` pattern: wrap the critical external call with a check that reverts on failure rather than silently continuing.

### Proof of Concept

1. Deploy an `EvmErc20` or `EvmErc20V2` token (or use an existing bridged token on Aurora).
2. Acquire a non-zero balance of that token.
3. Call `withdrawToNear(bytes("\xff\xff\xff"), amount)` — the recipient `\xff\xff\xff` is not a valid NEAR account ID.
4. The `ExitToNear` precompile at `0xe9217bc70b7ed1f598ddd3199e80b093fa71124f` returns failure (exit error `ERR_INVALID_RECEIVER_ACCOUNT_ID`).
5. The assembly `call` returns `res = 0`.
6. Because `res` is never checked, the function returns normally.
7. The caller's token balance is reduced by `amount` (burned), but no NEAR transfer was initiated.
8. The tokens are permanently lost. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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
