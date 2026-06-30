### Title
Unchecked Exit-Precompile Return Value After `_burn` in `withdrawToNear`/`withdrawToEthereum` Causes Permanent Fund Freeze — (`etc/eth-contracts/contracts/EvmErc20.sol`, `etc/eth-contracts/contracts/EvmErc20V2.sol`)

---

### Summary

Both `EvmErc20.sol` and `EvmErc20V2.sol` implement `withdrawToNear` and `withdrawToEthereum` by first calling `_burn` to destroy the caller's ERC-20 tokens, then invoking the exit precompile via inline assembly. The return value of the assembly `call` is captured in a local variable `res` but is **never checked**. If the precompile call fails for any reason, the ERC-20 tokens are permanently destroyed while no NEAR or ETH is ever released to the user — a direct analog to the reported burn-without-active-state-check pattern.

---

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
        // res is NEVER checked
    }
}
``` [1](#0-0) 

And identically in `EvmErc20V2.sol`: [2](#0-1) 

The same pattern applies to `withdrawToEthereum` in both contracts: [3](#0-2) [4](#0-3) 

The `ExitToNear` precompile (`engine-precompiles/src/native.rs`) can legitimately fail and return an `ExitError` in multiple cases — for example when the precompile is paused, when the recipient account ID is invalid, or when insufficient gas is forwarded. [5](#0-4) 

When the precompile returns an error, the EVM `call` opcode returns `0` into `res`. Because `res` is never inspected, the Solidity function does **not** revert. The `_burn` that already executed is not rolled back. The user's ERC-20 balance is permanently zeroed, but the corresponding NEP-141 tokens on NEAR (or ETH on Ethereum) are never released.

The structural parallel to the reported issue is exact:
- **Reported**: `burn` deletes all token state (including rental deposits) without checking for active rentals → renters' funds stuck.
- **This finding**: `_burn` destroys ERC-20 tokens without verifying the exit precompile succeeded → user's bridged funds permanently frozen.

---

### Impact Explanation

**Permanent freezing of funds (Critical).**

A user who calls `withdrawToNear` or `withdrawToEthereum` while the exit precompile is in a failing state (paused, invalid input, out-of-gas) will have their ERC-20 tokens burned with no recovery path. The NEP-141 tokens remain locked in the eth-connector contract on NEAR, and the ERC-20 supply on Aurora is reduced — creating an irreversible accounting discrepancy and a permanent loss for the user.

---

### Likelihood Explanation

Multiple realistic failure paths exist for the precompile call:

1. **Precompile paused**: The engine exposes `pause_precompiles` callable by the owner. If the `ExitToNear` or `ExitToEthereum` precompile is paused for maintenance, any user who calls `withdrawToNear`/`withdrawToEthereum` during that window loses their tokens permanently. [6](#0-5) 

2. **Invalid recipient**: A user supplying a malformed NEAR account ID causes the precompile to return `ERR_INVALID_RECEIVER_ACCOUNT_ID`. The `_burn` is not reverted. [7](#0-6) 

3. **Out of gas**: If the caller provides insufficient gas, the inner `call` can fail silently.

All three paths are reachable by an ordinary EVM user with no special privileges.

---

### Recommendation

Check the return value of the assembly `call` and revert if it indicates failure:

```solidity
assembly {
    let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
    if iszero(res) { revert(0, 0) }
}
```

This must be applied to all four functions: `withdrawToNear` and `withdrawToEthereum` in both `EvmErc20.sol` and `EvmErc20V2.sol`. Alternatively, restructure the functions to call the precompile first and only burn on confirmed success.

---

### Proof of Concept

1. Deploy an `EvmErc20` token (bridged NEP-141).
2. Owner calls `pause_precompiles` to pause the `ExitToNear` precompile.
3. User calls `withdrawToNear(recipient, amount)` with a valid recipient and a non-zero balance.
4. `_burn` executes, reducing the user's ERC-20 balance to zero.
5. The assembly `call` to the paused precompile returns `0`; `res` is ignored; the function returns normally.
6. The user's ERC-20 tokens are gone. The NEP-141 tokens remain locked in the connector. No refund path exists. [1](#0-0) [2](#0-1)

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

**File:** engine-precompiles/src/native.rs (L406-417)
```rust
        if let Some(target_gas) = target_gas
            && required_gas > target_gas
        {
            return Err(ExitError::OutOfGas);
        }

        // It's not allowed to call exit precompiles in static mode
        if is_static {
            return Err(ExitError::Other(Cow::from("ERR_INVALID_IN_STATIC")));
        } else if context.address != exit_to_near::ADDRESS.raw() {
            return Err(ExitError::Other(Cow::from("ERR_INVALID_IN_DELEGATE")));
        }
```

**File:** engine-precompiles/src/native.rs (L970-974)
```rust
            _ => {
                return Err(ExitError::Other(Cow::from(
                    "ERR_INVALID_RECEIVER_ACCOUNT_ID",
                )));
            }
```

**File:** engine/src/lib.rs (L309-315)
```rust
    pub extern "C" fn factory_update() {
        let io = Runtime;
        let env = Runtime;
        contract_methods::xcc::factory_update(io, &env)
            .map_err(ContractError::msg)
            .sdk_unwrap();
    }
```
