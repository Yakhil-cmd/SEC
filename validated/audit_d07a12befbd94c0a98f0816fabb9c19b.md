### Title
Unvalidated Exit Precompile Call Return Value Causes Permanent Token Burn Without Bridge Transfer - (`etc/eth-contracts/contracts/EvmErc20.sol`, `etc/eth-contracts/contracts/EvmErc20V2.sol`)

---

### Summary

Both `EvmErc20.sol` and `EvmErc20V2.sol` implement `withdrawToNear()` and `withdrawToEthereum()` by first burning the caller's ERC-20 tokens via `_burn()`, then invoking the Aurora exit precompile via an inline assembly `call()`. The return value `res` of that `call()` is captured in a local assembly variable but is **never checked**. If the precompile call fails, the function returns successfully from the EVM's perspective while the tokens have already been permanently destroyed and no NEP-141 transfer is ever scheduled on the NEAR side.

---

### Finding Description

In `EvmErc20.sol`, `withdrawToNear` executes as follows:

```solidity
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);          // tokens destroyed here — irreversible

    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    uint input_size = 1 + 32 + recipient.length;

    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        // res is never checked — no revert on failure
    }
}
``` [1](#0-0) 

The identical pattern exists in `withdrawToEthereum` (targeting the `ExitToEthereum` precompile at `0xb0bd02f6...`): [2](#0-1) 

And both functions are reproduced verbatim in `EvmErc20V2.sol`: [3](#0-2) [4](#0-3) 

The `ExitToNear` precompile (`engine-precompiles/src/native.rs`) returns `Err(ExitError)` — which the EVM translates to a subcall return value of `0` — in multiple reachable conditions:

- `ExitError::OutOfGas` when `required_gas > target_gas` (gas-limited call context)
- `ERR_INVALID_IN_STATIC` / `ERR_INVALID_IN_DELEGATE` when called from a static or delegatecall context
- Parsing failure from `ExitToNearParams::try_from` when the `recipient` bytes do not form a valid NEAR account ID
- `ERR_ETH_ATTACHED_FOR_ERC20_EXIT` if `apparent_value != 0`
- `get_nep141_from_erc20` failure if the ERC-20 is not registered in the bridge mapping [5](#0-4) 

When any of these conditions occur, the low-level `call()` returns `0` into `res`. Because `res` is never tested and no `revert` is issued, the Solidity function returns normally. The `_burn` that already executed is not rolled back. The tokens are gone, and no NEAR-side promise is ever created.

---

### Impact Explanation

**Critical — Permanent freezing/destruction of user funds.**

The ERC-20 tokens are burned from the user's balance with no possibility of recovery. The corresponding NEP-141 tokens held by the Aurora engine contract are never transferred to the intended recipient. The user loses the full `amount` of bridged tokens permanently. There is no admin escape hatch or recovery path once `_burn` has committed.

---

### Likelihood Explanation

**Medium-High.** The trigger conditions are fully user-controlled:

1. **Invalid recipient bytes**: A caller who passes a `recipient` byte array that does not decode to a valid NEAR account ID (e.g., empty bytes, bytes containing characters outside `[a-z0-9_\-.]`, or a string exceeding 64 characters) will cause `parse_recipient` inside `ExitToNearParams::try_from` to return `ExitError`, silently failing the precompile call while the burn succeeds.
2. **Gas exhaustion**: A contract that calls `withdrawToNear` with a forwarded gas limit below `EXIT_TO_NEAR_GAS` will trigger `ExitError::OutOfGas` from the precompile, again silently.

Both scenarios require only a standard ERC-20 token balance and a call to a public function — no privileged access is needed.

---

### Recommendation

Check the return value of the assembly `call()` and revert if it is zero, mirroring the pattern used in the original M-08 mitigation:

```solidity
assembly {
    let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
    if iszero(res) { revert(0, 0) }
}
```

Apply this fix to all four affected assembly blocks across `EvmErc20.sol` and `EvmErc20V2.sol`. Because `_burn` is called before the precompile invocation, any precompile failure must cause a full revert to restore the caller's token balance.

---

### Proof of Concept

1. Deploy `EvmErc20` mapped to a NEP-141 token. Mint `1000` tokens to `alice`.
2. `alice` calls `withdrawToNear(bytes("!!invalid account!!"), 1000)`.
3. `_burn(alice, 1000)` executes — alice's ERC-20 balance drops to 0.
4. The assembly `call()` to the `ExitToNear` precompile fails because `"!!invalid account!!"` is not a valid NEAR account ID; the precompile returns `ExitError` → subcall returns `0`.
5. `res` is never checked; the function returns without reverting.
6. Alice's 1000 ERC-20 tokens are permanently destroyed. The NEP-141 balance held by the Aurora engine contract is unchanged — no transfer was scheduled.
7. Net result: 1000 tokens permanently lost, with no recovery path. [1](#0-0) [6](#0-5)

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

**File:** engine-precompiles/src/native.rs (L404-419)
```rust
        let required_gas = Self::required_gas(input)?;

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

        let exit_to_near_params = ExitToNearParams::try_from(input)?;
```
