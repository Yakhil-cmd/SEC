### Title
Unchecked Return Value of Exit Precompile `call()` After Irreversible `_burn()` Causes Permanent Fund Loss - (File: `etc/eth-contracts/contracts/EvmErc20.sol`, `etc/eth-contracts/contracts/EvmErc20V2.sol`)

### Summary

In both `EvmErc20.sol` and `EvmErc20V2.sol`, the `withdrawToNear` and `withdrawToEthereum` functions first irreversibly burn the caller's ERC-20 tokens via `_burn()`, then invoke the Aurora exit precompile via inline assembly `call()`. The return value `res` of that `call()` is captured but **never checked**. If the precompile call fails (returns `0`), the transaction does not revert, the tokens are permanently destroyed on Aurora, and no corresponding NEP-141 or ETH credit is issued on the destination chain.

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
        // res is NEVER checked — silent failure
    }
}
``` [1](#0-0) 

The same pattern exists in `withdrawToEthereum` in `EvmErc20.sol`: [2](#0-1) 

And identically in both functions of `EvmErc20V2.sol`: [3](#0-2) 

The exit precompiles (`ExitToNear` at `0xe9217bc70b7ed1f598ddd3199e80b093fa71124f` and `ExitToEthereum` at `0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab`) can legitimately return failure (`0`) in multiple documented code paths:

- `ExitToNear::run()` returns `Err(ExitError::Other(...))` for `ERR_INVALID_IN_STATIC`, `ERR_INVALID_IN_DELEGATE`, invalid recipient account ID parsing, and other input validation failures. [4](#0-3) 
- `ExitToEthereum::run()` returns `Err(...)` for `ERR_INVALID_RECIPIENT_ADDRESS`, `ERR_ETH_ATTACHED_FOR_ERC20_EXIT`, and input size validation failures. [5](#0-4) 

When the EVM precompile returns an error, the low-level `call()` opcode returns `0`. Because `res` is never inspected and no `if iszero(res) { revert(...) }` guard exists, the Solidity function returns successfully despite the precompile having failed. The `_burn()` that already executed is not rolled back.

### Impact Explanation

**Critical — Permanent freezing/destruction of user funds.**

The sequence is:
1. User calls `withdrawToNear(recipient, amount)` or `withdrawToEthereum(recipient, amount)`.
2. `_burn()` destroys `amount` tokens from the user's balance on Aurora. This state change is committed.
3. The precompile `call()` fails silently (returns `0`).
4. No NEAR promise is scheduled; no NEP-141 tokens are credited on NEAR; no ETH is released on Ethereum.
5. The transaction succeeds from the EVM's perspective. The tokens are gone with no recourse.

The burned tokens represent real bridged assets (NEP-141 tokens locked on NEAR). Their permanent destruction on Aurora without a corresponding release on NEAR constitutes direct, irreversible theft/loss of user funds.

### Likelihood Explanation

**Medium.** Any token holder can trigger this path by:
- Providing a malformed or too-long NEAR recipient account ID (causing the precompile's input parser to return an error).
- Providing an `amount` that encodes to an invalid `U256` (overflow path in `parse_amount`). [6](#0-5) 
- Calling from a context where the precompile's `context.address` check fails (delegate call scenario). [7](#0-6) 

No special privileges are required. Any ERC-20 token holder interacting with the bridge withdrawal interface is exposed.

### Recommendation

Check the return value of the assembly `call()` and revert if it is `0`. For example, in both `withdrawToNear` and `withdrawToEthereum` in both `EvmErc20.sol` and `EvmErc20V2.sol`:

```solidity
assembly {
    let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
    if iszero(res) { revert(0, 0) }
}
```

This ensures that if the exit precompile fails for any reason, the entire transaction reverts, rolling back the `_burn()` and preserving the user's token balance.

### Proof of Concept

1. Deploy `EvmErc20` on Aurora with a valid NEP-141 backing token.
2. Mint tokens to `attacker` address.
3. Call `withdrawToNear(bytes("invalid account id !!!"), amount)` — the NEAR account ID contains characters invalid per NEAR's account ID rules, causing `ExitToNear::run()` to return `Err(ExitError::Other(...))`.
4. The assembly `call()` returns `0`. Since `res` is never checked, no revert occurs.
5. Observe: `attacker`'s ERC-20 balance is now `0` (tokens burned), but no NEP-141 tokens were credited on NEAR. The funds are permanently lost. [1](#0-0) [8](#0-7)

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

**File:** etc/eth-contracts/contracts/EvmErc20V2.sol (L53-77)
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

**File:** engine-precompiles/src/native.rs (L412-419)
```rust
        // It's not allowed to call exit precompiles in static mode
        if is_static {
            return Err(ExitError::Other(Cow::from("ERR_INVALID_IN_STATIC")));
        } else if context.address != exit_to_near::ADDRESS.raw() {
            return Err(ExitError::Other(Cow::from("ERR_INVALID_IN_DELEGATE")));
        }

        let exit_to_near_params = ExitToNearParams::try_from(input)?;
```

**File:** engine-precompiles/src/native.rs (L864-879)
```rust
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
```

**File:** engine-precompiles/src/native.rs (L1070-1075)
```rust
    #[test]
    #[should_panic(expected = "ERR_INVALID_AMOUNT")]
    fn test_exit_with_invalid_amount() {
        let input = (U256::from(u128::MAX) + 1).to_big_endian();
        parse_amount(input.as_slice()).unwrap();
    }
```
