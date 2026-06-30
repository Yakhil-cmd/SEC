### Title
Unchecked Precompile Call Return Value in `withdrawToNear` and `withdrawToEthereum` Causes Permanent Token Loss - (File: `etc/eth-contracts/contracts/EvmErc20.sol`, `etc/eth-contracts/contracts/EvmErc20V2.sol`)

---

### Summary

Both `EvmErc20.sol` and `EvmErc20V2.sol` burn the caller's ERC-20 tokens before invoking the Aurora bridge precompile via inline assembly. The return value of the `call()` opcode is captured into a local variable `res` but is **never checked**. If the precompile call fails (returns 0), the burn is not reverted and the user permanently loses their tokens with no corresponding NEP-141 credit on NEAR.

---

### Finding Description

In `EvmErc20.sol`, both `withdrawToNear` and `withdrawToEthereum` follow this pattern:

```solidity
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);          // tokens burned here

    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    uint input_size = 1 + 32 + recipient.length;

    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        // res is NEVER checked
    }
}
``` [1](#0-0) 

The identical pattern appears in `withdrawToEthereum`: [2](#0-1) 

And in both functions of `EvmErc20V2.sol`: [3](#0-2) [4](#0-3) 

The precompile at `0xe9217bc70b7ed1f598ddd3199e80b093fa71124f` (`ExitToNear`) and `0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab` (`ExitToEthereum`) are implemented in `engine-precompiles/src/native.rs` and can legitimately return failure (EVM `call` returns 0) in multiple documented conditions: [5](#0-4) 

Failure conditions include: precompile is paused (`ERR_PAUSED`), called in static context, called via delegate, out of gas, invalid recipient account ID, or any internal error in the precompile logic. The pause mechanism is explicitly supported: [6](#0-5) 

Under standard EVM semantics, a failed sub-`call` does **not** automatically revert the caller's state changes. The `_burn` that already executed reduces the user's ERC-20 balance permanently. Since `res` is never checked and no `require(res != 0)` follows, the function returns successfully despite the bridge operation having failed.

---

### Impact Explanation

**Critical — Permanent freezing of funds.**

A user calling `withdrawToNear` or `withdrawToEthereum` on any `EvmErc20`/`EvmErc20V2` token (all NEP-141-bridged ERC-20 tokens on Aurora) will have their ERC-20 balance burned. If the precompile call fails silently, the corresponding NEP-141 tokens are never transferred to the recipient. The burned ERC-20 tokens cannot be recovered (no mint-back path exists), and the NEP-141 tokens remain locked in the Aurora engine contract with no mechanism to claim them. This constitutes permanent loss of user funds.

---

### Likelihood Explanation

**Medium.** The precompile can fail due to:
1. The precompile being paused by the admin (a supported operational state).
2. Out-of-gas conditions in the precompile execution path.
3. Invalid or oversized recipient account IDs passed by the caller.
4. Any future error path added to the precompile.

Any token holder can trigger this path without special privileges by calling `withdrawToNear` or `withdrawToEthereum` directly. The burn-before-call ordering makes every invocation a potential fund-loss event if the precompile is in a failure state.

---

### Recommendation

Check the return value of the assembly `call` and revert if it is zero:

```solidity
assembly {
    let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
    if iszero(res) { revert(0, 0) }
}
```

Apply this fix to all four assembly blocks across `EvmErc20.sol` and `EvmErc20V2.sol`. This ensures that if the precompile call fails, the entire transaction (including the `_burn`) is reverted, preserving the user's token balance.

---

### Proof of Concept

1. Deploy `EvmErc20` with a NEP-141 backing token.
2. Pause the `ExitToNear` precompile via the engine's admin pause mechanism.
3. Call `withdrawToNear(recipient, amount)` as a token holder.
4. Observe: `_burn` executes, reducing the caller's ERC-20 balance by `amount`. The assembly `call` to the paused precompile returns 0. `res` is never checked. The function returns without reverting.
5. Result: The caller's ERC-20 tokens are permanently destroyed. No NEP-141 tokens are transferred. The funds are irrecoverably lost.

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

**File:** engine-precompiles/src/native.rs (L412-417)
```rust
        // It's not allowed to call exit precompiles in static mode
        if is_static {
            return Err(ExitError::Other(Cow::from("ERR_INVALID_IN_STATIC")));
        } else if context.address != exit_to_near::ADDRESS.raw() {
            return Err(ExitError::Other(Cow::from("ERR_INVALID_IN_DELEGATE")));
        }
```

**File:** engine/src/pausables.rs (L13-16)
```rust
    pub struct PrecompileFlags: u32 {
        const EXIT_TO_NEAR        = 0b01;
        const EXIT_TO_ETHEREUM    = 0b10;
    }
```
