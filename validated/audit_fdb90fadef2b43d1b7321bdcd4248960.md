### Title
Unchecked Exit-Precompile Return Value After Token Burn Causes Permanent Fund Loss - (File: etc/eth-contracts/contracts/EvmErc20.sol)

### Summary

`EvmErc20.withdrawToNear` and `EvmErc20.withdrawToEthereum` burn the caller's tokens before invoking the Aurora exit precompile via a low-level assembly `call`. The return value of that `call` is captured in a local variable but **never inspected**. If the precompile call fails for any reason, the burn is irreversible and the user's funds are permanently destroyed with no corresponding NEAR or Ethereum transfer.

### Finding Description

Both withdrawal functions in `EvmErc20.sol` follow the same two-step pattern:

1. **Burn tokens** — `_burn(_msgSender(), amount)` is executed unconditionally and commits the state change.
2. **Call exit precompile** — a low-level assembly `call` is made to the precompile address. The result (`res`) is stored but never checked, and the function never reverts on failure.

```solidity
// withdrawToNear (lines 53-63)
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);                          // state change committed

    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    uint input_size = 1 + 32 + recipient.length;

    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f,
                        0, add(input, 32), input_size, 0, 32)
        // res is never checked — silent failure
    }
}
```

The `ExitToNear` precompile (`engine-precompiles/src/native.rs`) can return an `ExitError` (causing the sub-call to return `0`) in several reachable conditions:

- **`amount > u128::MAX`** — `parse_amount` explicitly rejects values exceeding `u128::MAX` even though the Solidity parameter is `uint256`.
- **Invalid recipient** — `parse_recipient` fails if the NEAR account-ID bytes are malformed.
- **Token not registered** — `get_nep141_from_erc20` returns `ERR_TARGET_TOKEN_NOT_FOUND` if the ERC-20 → NEP-141 mapping is absent.

In EVM semantics, a failed sub-call reverts only the sub-call's own state; the caller's prior `_burn` is **not** rolled back. Because `withdrawToNear` neither checks `res` nor reverts, the function returns successfully while the user's tokens are gone.

The identical pattern exists in `withdrawToEthereum` (lines 65-76), which calls the `ExitToEthereum` precompile at `0xb0bd02f6...`.

### Impact Explanation

Any user who calls `withdrawToNear` or `withdrawToEthereum` under a condition that causes the precompile to fail will have their ERC-20 tokens permanently burned with no corresponding asset transfer. This constitutes **permanent freezing (destruction) of user funds** — a Critical impact.

The most straightforward trigger is passing `amount > type(uint128).max`. The Solidity function accepts a `uint256`, so no client-side validation prevents this. The burn succeeds; the precompile rejects the oversized amount; the assembly call silently returns `0`; the function returns normally; the tokens are gone.

### Likelihood Explanation

The trigger conditions are reachable by any unprivileged token holder:

- Passing an amount larger than `2^128 - 1` is a simple arithmetic mistake or deliberate input.
- Providing a malformed NEAR account-ID string (e.g., an empty byte array or one containing invalid characters) is equally trivial.

No special privileges, governance access, or external oracle compromise is required. The function is a standard public ERC-20 bridge exit that any token holder is expected to call.

### Recommendation

Validate the amount and recipient **before** burning, and revert if the precompile call fails:

```solidity
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    require(amount <= type(uint128).max, "amount exceeds u128");

    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    uint input_size = 1 + 32 + recipient.length;

    bool success;
    assembly {
        success := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f,
                        0, add(input, 32), input_size, 0, 32)
    }
    require(success, "exit precompile call failed");

    _burn(_msgSender(), amount);   // burn only after confirming the precompile accepted the call
}
```

Alternatively, move the burn into the precompile callback path so it only executes on confirmed success.

### Proof of Concept

1. Deploy or interact with an existing `EvmErc20` token on Aurora.
2. Acquire a balance of at least `1` token unit.
3. Call `withdrawToNear(recipient_bytes, 2**128)` (amount exceeds `u128::MAX`).
4. Observe: `_burn` succeeds, the ERC-20 balance decreases by `2^128`, the assembly `call` to the exit precompile returns `0` (because `parse_amount` in `native.rs` rejects the value), the function returns without reverting.
5. Result: tokens are permanently destroyed; no NEAR transfer is scheduled.

The same outcome is reproducible with `withdrawToEthereum` using the same oversized amount, or with either function using a recipient that fails `parse_recipient` validation in the precompile. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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
