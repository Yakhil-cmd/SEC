### Title
Unchecked Precompile Call Return Value After Irreversible Token Burn Causes Permanent Fund Loss - (`etc/eth-contracts/contracts/EvmErc20.sol`)

### Summary
In `EvmErc20.sol` and `EvmErc20V2.sol`, the `withdrawToNear` and `withdrawToEthereum` functions burn the caller's ERC-20 tokens before invoking the exit precompile via inline assembly. The return value of the `call` opcode is captured in a local variable `res` but is **never checked**. If the precompile call fails, the burn is not reverted, permanently destroying the user's tokens with no corresponding NEAR or Ethereum withdrawal.

### Finding Description

In `withdrawToNear`:

```solidity
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);          // <-- irreversible burn happens first

    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    uint input_size = 1 + 32 + recipient.length;

    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        // res is NEVER checked — silent failure is possible
    }
}
``` [1](#0-0) 

The same pattern exists in `withdrawToEthereum` and in both functions of `EvmErc20V2.sol`: [2](#0-1) [3](#0-2) 

The EVM `call` opcode returns `0` on failure. Because `res` is never inspected and no `revert` is issued on failure, the function returns successfully even when the precompile rejects the call. The `_burn` that preceded the call is **not rolled back** — it is a completed state change in the ERC-20 contract's own storage.

The exit-to-near precompile (`0xe9217bc70b7ed1f598ddd3199e80b093fa71124f`) can fail for user-controlled reasons, including a malformed or oversized `recipient` byte string. The precompile validates the recipient as a NEAR account ID; an invalid account ID causes the precompile to revert its own effects, but the ERC-20 burn in the calling contract is already committed.

### Impact Explanation

**Critical — Permanent freezing of funds.**

A user who calls `withdrawToNear` with a recipient that the precompile rejects (e.g., an invalid NEAR account ID, an empty byte string, or a byte string exceeding NEAR's account ID length limit) will have their ERC-20 tokens permanently burned with no NEAR tokens transferred. The tokens are unrecoverable: the ERC-20 balance is gone, and no NEAR-side credit is issued.

### Likelihood Explanation

**Medium.** The `recipient` parameter is a raw `bytes memory` value supplied entirely by the caller. There is no on-chain validation of the recipient before the burn. A user who mistakenly encodes an invalid NEAR account ID (e.g., an empty string, a string with illegal characters, or a string longer than 64 bytes) will trigger the failure path. Additionally, if the exit precompile is paused via Aurora's pause mechanism, every call to `withdrawToNear` would silently burn tokens with no withdrawal, affecting all users of the token during the pause window.

### Recommendation

Check the return value of the assembly `call` and revert on failure, **before** the burn, or restructure so the burn only occurs after confirming the precompile accepted the call:

```solidity
assembly {
    let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
    if iszero(res) { revert(0, 0) }
}
```

Apply the same fix to `withdrawToEthereum` in both `EvmErc20.sol` and `EvmErc20V2.sol`. [1](#0-0) [4](#0-3) 

### Proof of Concept

1. User holds 1000 units of a bridged ERC-20 token deployed via `EvmErc20.sol`.
2. User calls `withdrawToNear(b"", 1000)` (empty recipient).
3. `_burn(msg.sender, 1000)` executes — user's ERC-20 balance drops to 0.
4. The exit-to-near precompile rejects the empty recipient and returns `0`.
5. `res` is `0` but is never checked; the function returns without reverting.
6. User's 1000 tokens are permanently destroyed. No NEAR is credited. Funds are frozen forever.

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
