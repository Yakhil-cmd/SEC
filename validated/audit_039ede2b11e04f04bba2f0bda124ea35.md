### Title
Unchecked Exit-Precompile Call Return Value After Token Burn Causes Permanent Fund Freeze - (File: `etc/eth-contracts/contracts/EvmErc20.sol`, `etc/eth-contracts/contracts/EvmErc20V2.sol`)

---

### Summary

Both `EvmErc20.sol` and `EvmErc20V2.sol` burn a user's ERC-20 tokens **before** calling the exit precompile via inline assembly, and never check the `call` return value. If the precompile call fails for any reason, the burn is irreversible and the corresponding NEP-141 tokens on NEAR are never released, permanently destroying the user's funds.

---

### Finding Description

In `EvmErc20.sol`, both `withdrawToNear` and `withdrawToEthereum` follow the same unsafe pattern:

```solidity
// EvmErc20.sol – withdrawToNear (lines 53-63)
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);          // ← tokens destroyed here, irreversibly

    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    uint input_size = 1 + 32 + recipient.length;

    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        // res is assigned but NEVER checked – silent failure is invisible
    }
}
```

The same pattern appears in `withdrawToEthereum` (lines 65-76) and in both functions of `EvmErc20V2.sol` (lines 53-77).

The EVM `call` opcode returns `0` on failure. Because `res` is never inspected after the assembly block, a failed precompile invocation does not revert the transaction. The `_burn` that already executed cannot be undone, so the user's ERC-20 balance is destroyed with no corresponding NEP-141 release on NEAR (or ETH unlock on Ethereum).

This is the direct analog to the reported SafeERC20 issue: just as calling `token.transfer()` without checking its return value silently swallows a failed transfer, calling the exit precompile via raw assembly without checking `res` silently swallows a failed bridge exit — after the irreversible burn has already occurred.

---

### Impact Explanation

**Permanent freezing / destruction of user funds (Critical).**

When the exit precompile call fails and `res == 0`:
- The ERC-20 tokens are already burned from the user's balance (state change committed).
- No NEP-141 `ft_transfer` promise is scheduled on NEAR.
- No Ethereum unlock is triggered.
- The transaction does not revert, so there is no on-chain signal of failure.
- The user has no recourse; the tokens are gone permanently.

---

### Likelihood Explanation

The `ExitToNear` precompile (`0xe9217bc70b7ed1f598ddd3199e80b093fa71124f`) returns `ExitError` for several reachable conditions visible in `engine-precompiles/src/native.rs`:
- Input size outside `[MIN_INPUT_SIZE, MAX_INPUT_SIZE]` (line 40 defines `MAX_INPUT_SIZE = 1_024`).
- Invalid or malformed recipient account ID.
- Missing NEP-141 mapping for the calling ERC-20 address.
- Out-of-gas at the precompile level.

Any unprivileged token holder can trigger this by passing a recipient string that is syntactically valid enough to pass Solidity-side encoding but fails the precompile's NEAR account-ID validation. The call path is entirely user-controlled and requires no special privilege.

---

### Recommendation

Check the return value of every low-level `call` to the exit precompile and revert if it fails, **before** burning tokens — or restructure so the burn only occurs after a confirmed successful precompile invocation:

```solidity
assembly {
    let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
    if iszero(res) { revert(0, 0) }
}
```

Alternatively, restructure the call order: attempt the precompile call first (in a way that can be reverted), and only burn on confirmed success.

---

### Proof of Concept

1. Deploy `EvmErc20` (or use an existing bridge token).
2. Acquire a non-zero balance of the ERC-20 token.
3. Call `withdrawToNear` with a `recipient` byte string that exceeds `MAX_INPUT_SIZE` (1 024 bytes) or contains characters that fail NEAR account-ID validation inside the precompile.
4. The `_burn` executes successfully, reducing the caller's balance to zero.
5. The assembly `call` to the precompile returns `0` (failure); `res` is never checked.
6. The transaction completes without revert.
7. The caller's ERC-20 tokens are permanently destroyed; no NEP-141 tokens are released on NEAR.

Relevant production code: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** engine-precompiles/src/native.rs (L37-40)
```rust
const MIN_INPUT_SIZE: usize = 3;
#[cfg(feature = "error_refund")]
const MIN_INPUT_SIZE: usize = 21;
const MAX_INPUT_SIZE: usize = 1_024;
```
