### Title
Unchecked Precompile Call Return Value in `withdrawToNear`/`withdrawToEthereum` Burns Tokens Without Bridging - (File: `etc/eth-contracts/contracts/EvmErc20.sol`, `etc/eth-contracts/contracts/EvmErc20V2.sol`)

### Summary
`EvmErc20` and `EvmErc20V2` burn a user's ERC-20 tokens before calling the exit precompile via inline assembly, but never check the return value of that `call`. If the precompile call fails for any reason, the tokens are permanently destroyed with no corresponding NEP-141 transfer on NEAR, resulting in permanent fund loss.

### Finding Description
Both `EvmErc20.sol` and `EvmErc20V2.sol` implement `withdrawToNear()` and `withdrawToEthereum()` with the same structural pattern:

1. `_burn(_msgSender(), amount)` — tokens are irreversibly destroyed.
2. An inline assembly `call` to the exit precompile address is made.
3. The return value `res` is captured into a local variable but **never checked or acted upon**.

In `EvmErc20.sol`:
```solidity
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);                          // tokens destroyed first
    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    uint input_size = 1 + 32 + recipient.length;
    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        // res is never checked — no revert if call fails
    }
}
``` [1](#0-0) 

The identical pattern appears in `withdrawToEthereum()` (calling `0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab`) and in both functions of `EvmErc20V2.sol`: [2](#0-1) [3](#0-2) 

When the EVM `call` opcode returns `0` (failure), Solidity inline assembly does not automatically revert. Because `_burn` has already executed and committed the balance reduction, the function returns successfully with the user's tokens gone and no bridge transfer initiated.

The `ExitToNear` precompile (`engine-precompiles/src/native.rs`) can fail with an `ExitError` for several reasons reachable without admin access: the NEP-141 mapping for the ERC-20 address is absent or corrupted (`get_nep141_from_erc20` returns an error), the input is malformed, or the precompile runs out of gas in a constrained execution context. [4](#0-3) 

### Impact Explanation
**Critical — Permanent freezing of funds.**

A user calling `withdrawToNear()` or `withdrawToEthereum()` on any `EvmErc20`/`EvmErc20V2` contract when the precompile call fails will have their ERC-20 tokens burned with zero recourse. There is no refund path: `_burn` is not conditional on the precompile succeeding, and there is no try/catch or revert after the assembly block. The tokens cease to exist on Aurora and no NEP-141 tokens are released on NEAR.

### Likelihood Explanation
**Medium.** Every bridged NEP-141 token on Aurora is represented by an `EvmErc20` or `EvmErc20V2` contract, and every user who withdraws tokens passes through this code path. The precompile call can fail if: (a) the NEP-141 ↔ ERC-20 mapping is missing or corrupted in storage, (b) the input encoding does not match what the currently deployed precompile version expects (e.g., `EvmErc20` uses `\x01 | amount | recipient` while `EvmErc20V2` uses `\x01 | sender | amount | recipient` — a version mismatch between contract and precompile silently fails), or (c) gas is exhausted inside the precompile. None of these require admin access; any token holder can trigger the path.

### Recommendation
Check the return value of the assembly `call` and revert if it is zero, so that the `_burn` is rolled back atomically:

```solidity
assembly {
    let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
    if iszero(res) { revert(0, 0) }
}
```

This ensures the burn and the bridge transfer are atomic: either both succeed or neither does, matching the SafeERC20 pattern recommended in the reference report.

### Proof of Concept
1. Deploy an `EvmErc20` contract whose NEP-141 mapping has been removed from storage (or simulate a precompile failure by exhausting gas before the assembly call).
2. Call `withdrawToNear(recipient, amount)` as a token holder with a non-zero balance.
3. Observe: `_burn` reduces the caller's balance to zero; the assembly `call` returns `0` (failure); the function returns without reverting.
4. Result: the caller's ERC-20 tokens are permanently destroyed, no NEP-141 tokens are transferred on NEAR, and the user has no mechanism to recover funds. [1](#0-0) [3](#0-2)

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

**File:** engine-precompiles/src/native.rs (L932-934)
```rust
                let erc20_address = context.caller;
                let nep141_address = get_nep141_from_erc20(erc20_address.as_bytes(), &self.io)?;
                let amount = parse_amount(&input[..32])?;
```
