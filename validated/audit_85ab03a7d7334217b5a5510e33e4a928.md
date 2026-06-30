### Title
Unhandled Return Value of Low-Level Precompile `call` in Withdrawal Functions — (`File: etc/eth-contracts/contracts/EvmErc20.sol`, `etc/eth-contracts/contracts/EvmErc20V2.sol`)

---

### Summary

Both `EvmErc20.sol` and `EvmErc20V2.sol` implement `withdrawToNear` and `withdrawToEthereum` functions that first burn the caller's tokens via `_burn`, then invoke an Aurora exit precompile via an inline `assembly { call(...) }` block. The return value of that low-level `call` is captured in a local variable `res` but is **never checked**. If the precompile call fails (returns 0), the burn is irreversible while the cross-chain withdrawal never occurs, permanently destroying the user's funds.

---

### Finding Description

In `EvmErc20.sol`:

```solidity
// withdrawToNear (lines 53–63)
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);          // tokens destroyed here
    ...
    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        // res is never checked — silent failure is possible
    }
}

// withdrawToEthereum (lines 65–76)
function withdrawToEthereum(address recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);          // tokens destroyed here
    ...
    assembly {
        let res := call(gas(), 0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab, 0, add(input, 32), input_size, 0, 32)
        // res is never checked — silent failure is possible
    }
}
```

The identical pattern exists in `EvmErc20V2.sol` at the same logical locations.

The sequence is:
1. `_burn` executes and permanently removes tokens from the EVM supply.
2. The low-level `call` to the exit precompile is made.
3. If the precompile returns `0` (failure), the assembly block does **not** revert.
4. The outer Solidity function returns normally.
5. The user's tokens are gone; no corresponding NEAR or Ethereum withdrawal is credited.

This is the direct analog of the reported `approve` unhandled-return-value class: a critical external call whose failure is silently swallowed after an irreversible state change.

---

### Impact Explanation

**Critical — Permanent freezing / direct destruction of user funds.**

If the exit precompile call fails for any reason, the user's ERC-20 tokens are permanently burned with no corresponding release on the destination chain. There is no retry mechanism, no revert, and no refund path. The funds are unrecoverable.

---

### Likelihood Explanation

**Medium.** Realistic failure scenarios for the precompile call include:

- The Aurora exit precompile is paused or administratively disabled at the time of the call (a documented operational mode).
- A bug or state inconsistency inside the precompile causes it to return `false` rather than reverting.
- The precompile consumes all forwarded gas internally and returns `0` due to out-of-gas.

Any of these conditions causes a silent failure that the contract cannot distinguish from success.

---

### Recommendation

Check the return value of the low-level `call` and revert if it indicates failure. Replace the unchecked assembly block with a checked pattern:

```solidity
assembly {
    let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
    if iszero(res) { revert(0, 0) }
}
```

Apply the same fix to all four assembly blocks across both `EvmErc20.sol` and `EvmErc20V2.sol`. Alternatively, restructure the functions to perform the precompile call **before** `_burn`, reverting on failure, so the burn only executes when the withdrawal is confirmed.

---

### Proof of Concept

1. Deploy `EvmErc20` with a normal admin.
2. Mint tokens to `user`.
3. Arrange for the exit precompile at `0xe9217bc70b7ed1f598ddd3199e80b093fa71124f` to return `0` (e.g., by calling from a context where the precompile is paused, or by simulating a precompile that returns false).
4. Call `withdrawToNear(recipient, amount)` as `user`.
5. Observe: `_burn` succeeds (user balance decreases to zero), the assembly `call` returns `0`, no revert occurs, transaction succeeds.
6. Result: `user` has lost `amount` tokens permanently; no NEAR-side credit is issued. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** etc/eth-contracts/contracts/EvmErc20V2.sol (L65-77)
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
