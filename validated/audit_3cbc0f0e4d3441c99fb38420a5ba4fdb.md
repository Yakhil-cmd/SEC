### Title
Unchecked Precompile Call Return Value After Irreversible `_burn` Enables Permanent Token Loss - (`etc/eth-contracts/contracts/EvmErc20.sol`, `etc/eth-contracts/contracts/EvmErc20V2.sol`)

---

### Summary

Both `EvmErc20.sol` and `EvmErc20V2.sol` implement `withdrawToNear` and `withdrawToEthereum` by first calling `_burn` (irreversible) and then invoking the exit precompile via inline assembly. The assembly `call` return value (`res`) is captured but **never checked**. If the precompile call fails for any reason — including gas exhaustion — the burn is committed and the tokens are permanently destroyed with no corresponding NEP-141 or Ethereum-side release.

---

### Finding Description

In `EvmErc20.sol`, both withdrawal functions follow this pattern:

```solidity
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);          // ← irreversible burn happens first

    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    uint input_size = 1 + 32 + recipient.length;

    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        // res is NEVER checked
    }
}
``` [1](#0-0) 

The identical pattern exists in `withdrawToEthereum`: [2](#0-1) 

And in both functions of `EvmErc20V2.sol`: [3](#0-2) [4](#0-3) 

The precompile dispatcher in `engine-precompiles/src/lib.rs` maps all `ExitError` returns from the precompile's `run()` function to `PrecompileFailure::Error`:

```rust
p.run(input, gas_limit.map(EthGas::new), context, is_static)
    .map_err(|exit_status| PrecompileFailure::Error { exit_status })
``` [5](#0-4) 

`PrecompileFailure::Error` causes the EVM `call` opcode to return `0` (failure) while allowing the calling contract's execution to continue — exactly the same as a failed external call. Since `res` is never checked, the outer function returns normally after the burn.

The `ExitToNear` precompile can return `ExitError` (not `ExitFatal`) in multiple paths, including:

- `ExitError::OutOfGas` when `required_gas > target_gas`
- `ExitError::Other("ERR_INVALID_IN_STATIC")` in static context
- `ExitError::Other("ERR_INVALID_IN_DELEGATE")` in delegatecall context
- `ExitError::Other("ERR_TARGET_TOKEN_NOT_FOUND")` if the ERC-20 has no NEP-141 mapping [6](#0-5) 

Note: the **paused precompile** path returns `PrecompileFailure::Fatal` (not `Error`), which causes the entire transaction to revert — that path is safe. The dangerous paths are those returning `PrecompileFailure::Error`.

The declared gas cost for the exit precompile is `0` (marked TODO), meaning the gas-limit check inside the precompile never fires: [7](#0-6) 

However, the precompile still consumes EVM gas during execution (storage reads, promise serialization). Due to EIP-150, the assembly `call(gas(), ...)` forwards at most 63/64 of remaining gas. If the caller provides a gas limit sufficient for `_burn` but insufficient for the precompile's actual execution, the precompile call fails with out-of-gas, `res = 0`, and the outer function returns normally — with the burn already committed.

---

### Impact Explanation

Any user whose `withdrawToNear` or `withdrawToEthereum` call results in a failed precompile sub-call (while the outer transaction succeeds) permanently loses their bridged tokens:

- ERC-20 balance is burned on Aurora (EVM side)
- No NEP-141 `ft_transfer` promise is scheduled on the NEAR side
- No Ethereum-side `withdraw` is initiated

The tokens are unrecoverable. This is **permanent freezing of funds** (Critical).

---

### Likelihood Explanation

The trigger is gas exhaustion in the precompile sub-call. A user or an intermediate contract calling `withdrawToNear` with a gas limit that is:
- High enough for `_burn` (~5,000 EVM gas for a storage write), but
- Too low for the precompile's actual execution (storage reads + promise log construction)

will silently lose their tokens. This is a realistic accidental scenario (e.g., a DApp estimating gas incorrectly) and also a deliberate griefing vector if a malicious contract calls `withdrawToNear` on behalf of a victim with a crafted gas limit. The `EXIT_TO_NEAR_GAS = 0` constant means no upfront gas check warns the caller.

---

### Recommendation

Add a `require` on the assembly return value in all four withdrawal functions in both `EvmErc20.sol` and `EvmErc20V2.sol`:

```solidity
assembly {
    let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
    if iszero(res) { revert(0, 0) }
}
```

This ensures that if the precompile call fails for any reason, the entire transaction reverts (including the `_burn`), preventing permanent token loss.

---

### Proof of Concept

1. Deploy `EvmErc20` (or `EvmErc20V2`) via the Aurora factory for a registered NEP-141 token.
2. Mint tokens to address `A`.
3. From address `A`, call `withdrawToNear(recipient, amount)` with a gas limit `G` such that:
   - `G` is sufficient to execute `_burn` (completes the storage write)
   - `(G - gas_for_burn) * 63/64` is less than the gas consumed by the `ExitToNear` precompile's storage reads and promise serialization
4. Observe: the transaction succeeds (EVM status = success), `A`'s ERC-20 balance is reduced by `amount`, but no NEP-141 transfer is initiated on NEAR.
5. The tokens are permanently lost — `A`'s ERC-20 balance is zero and no NEP-141 tokens arrive at `recipient`.

The root cause is confirmed at: [8](#0-7) [9](#0-8)

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

**File:** engine-precompiles/src/lib.rs (L164-175)
```rust
fn process_precompile(
    p: &dyn Precompile,
    handle: &impl PrecompileHandle,
) -> Result<PrecompileOutput, PrecompileFailure> {
    let input = handle.input();
    let gas_limit = handle.gas_limit();
    let context = handle.context();
    let is_static = handle.is_static();

    p.run(input, gas_limit.map(EthGas::new), context, is_static)
        .map_err(|exit_status| PrecompileFailure::Error { exit_status })
}
```

**File:** engine-precompiles/src/native.rs (L46-49)
```rust
    pub(super) const EXIT_TO_NEAR_GAS: EthGas = EthGas::new(0);

    // TODO(#483): Determine the correct amount of gas
    pub(super) const EXIT_TO_ETHEREUM_GAS: EthGas = EthGas::new(0);
```

**File:** engine-precompiles/src/native.rs (L404-417)
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
```
