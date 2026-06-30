### Title
Unchecked Precompile Call Return Value in `withdrawToNear`/`withdrawToEthereum` Causes Permanent Token Loss - (`etc/eth-contracts/contracts/EvmErc20.sol`, `etc/eth-contracts/contracts/EvmErc20V2.sol`)

---

### Summary

Both `EvmErc20.sol` and `EvmErc20V2.sol` burn the caller's ERC-20 tokens **before** calling the Aurora exit precompile via inline assembly. The return value of the `call` opcode is captured in a local variable `res` but is **never checked**. If the precompile call fails for any reason, the ERC-20 tokens are permanently destroyed while the corresponding NEP-141 tokens remain locked inside the Aurora contract — a permanent, irrecoverable loss of user funds.

---

### Finding Description

In both `EvmErc20.sol` and `EvmErc20V2.sol`, the `withdrawToNear` and `withdrawToEthereum` functions follow this pattern:

1. Call `_burn(sender, amount)` — irreversibly destroys the user's ERC-20 tokens.
2. Encode calldata for the exit precompile.
3. Call the precompile via inline assembly.
4. **Silently ignore the return value.**

`EvmErc20.sol` `withdrawToNear` (lines 53–63):
```solidity
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);
    ...
    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
    }
}
```

`EvmErc20.sol` `withdrawToEthereum` (lines 65–76):
```solidity
function withdrawToEthereum(address recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);
    ...
    assembly {
        let res := call(gas(), 0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab, 0, add(input, 32), input_size, 0, 32)
    }
}
```

The same pattern is present in `EvmErc20V2.sol` at lines 53–77.

In the EVM, the `call` opcode returns `0` when the callee reverts or returns an error. The `ExitToNear` and `ExitToEthereum` precompiles can and do return `ExitError` in multiple reachable conditions (e.g., `ERR_TARGET_TOKEN_NOT_FOUND`, `ERR_INVALID_RECIPIENT_ADDRESS`, `ERR_ETH_ATTACHED_FOR_ERC20_EXIT`, etc.). When this happens, `res` is `0`, but since it is never checked and no `revert` is issued, the Solidity function returns successfully. The burn has already committed; there is no rollback.

---

### Impact Explanation

**Critical — Permanent freezing / direct theft of user funds.**

The user's ERC-20 tokens are burned (removed from circulation on Aurora) but the corresponding NEP-141 tokens are never released from the Aurora contract's custody on NEAR. The user loses 100% of the withdrawn amount with no recovery path. This matches the "permanent freezing of funds" impact class.

---

### Likelihood Explanation

**Medium-High.** The precompile can fail for several reasons reachable by ordinary users:

- Passing a malformed or too-long recipient account ID causes `ERR_INVALID_RECEIVER_ACCOUNT_ID`.
- Calling `withdrawToNear` on an ERC-20 whose NEP-141 mapping has not been registered triggers `ERR_TARGET_TOKEN_NOT_FOUND`.
- Any future precompile-level pause or state inconsistency silently swallows the failure.

No special privileges are required; any token holder can trigger the path by calling `withdrawToNear` or `withdrawToEthereum` directly.

---

### Recommendation

After the assembly `call`, check `res` and revert if it is `0`:

```solidity
assembly {
    let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
    if iszero(res) { revert(0, 0) }
}
```

Apply the same fix to both `withdrawToNear` and `withdrawToEthereum` in both `EvmErc20.sol` and `EvmErc20V2.sol`. Because the burn must be atomic with the precompile call, the revert must happen before the transaction finalises — which the assembly-level `revert` achieves.

---

### Proof of Concept

**`EvmErc20.sol` — `withdrawToNear`** (burn before unchecked call): [1](#0-0) 

**`EvmErc20.sol` — `withdrawToEthereum`** (same pattern): [2](#0-1) 

**`EvmErc20V2.sol` — `withdrawToNear`** (identical unchecked call): [3](#0-2) 

**`EvmErc20V2.sol` — `withdrawToEthereum`**: [4](#0-3) 

**Reachable `ExitError` paths in the `ExitToNear` precompile** (confirming the precompile can fail): [5](#0-4) 

**Attack flow:**

1. User holds ERC-20 tokens on Aurora (bridged from NEP-141).
2. User calls `withdrawToNear(recipient, amount)` with a recipient that triggers a precompile error (e.g., an unregistered NEP-141 mapping).
3. `_burn(sender, amount)` executes — tokens destroyed.
4. Precompile call returns `0` (failure); `res` is never checked; no revert.
5. Transaction succeeds. ERC-20 tokens are gone; NEP-141 tokens remain locked in Aurora. Funds are permanently lost.

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

**File:** engine-precompiles/src/native.rs (L576-580)
```rust
    if context.apparent_value != U256::zero() {
        return Err(ExitError::Other(Cow::from(
            "ERR_ETH_ATTACHED_FOR_ERC20_EXIT",
        )));
    }
```
