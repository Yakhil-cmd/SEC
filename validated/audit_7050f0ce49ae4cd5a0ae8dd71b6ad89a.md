Now I have all the information needed. Let me analyze the analog carefully.

The report's vulnerability class is: **connector/bridge accounting bug** — two separate accounting systems tracking the same asset, where one operation updates only one side, creating irrecoverable split state.

In Aurora Engine, the analog is in `EvmErc20.sol` and `EvmErc20V2.sol`. Both `withdrawToNear` and `withdrawToEthereum` call `_burn(...)` first (updating EVM ERC-20 balance), then invoke the exit precompile via inline assembly — but **never check the return value `res`**. If the precompile call fails with a non-fatal `ExitError` (e.g., `OutOfGas`), the `call()` opcode returns 0, the assembly block exits silently, and the function returns successfully. The EVM balance is permanently decremented while the NEAR-side NEP-141 release never occurs.

---

### Title
Unchecked Exit-Precompile Return Value in `withdrawToNear`/`withdrawToEthereum` Causes Permanent EVM–NEAR Accounting Split — (File: `etc/eth-contracts/contracts/EvmErc20.sol`, `etc/eth-contracts/contracts/EvmErc20V2.sol`)

### Summary
`EvmErc20` and `EvmErc20V2` burn EVM mirror tokens before calling the `ExitToNear` / `ExitToEthereum` precompile. The assembly `call()` return value is captured in `res` but never inspected. A non-fatal precompile failure (e.g., `ExitError::OutOfGas`) causes the `call()` to return 0 without reverting the outer frame. The burn is committed, the NEAR-side transfer is never scheduled, and the user's funds are permanently destroyed.

### Finding Description
Both contracts share the same vulnerable pattern in `withdrawToNear` and `withdrawToEthereum`:

**`EvmErc20.sol` lines 53–63 (`withdrawToNear`):**
```solidity
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);          // ← EVM balance decremented here

    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    uint input_size = 1 + 32 + recipient.length;

    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        // res is NEVER checked — silent failure path exists
    }
}
```

The same unchecked pattern appears in `withdrawToEthereum` (lines 65–76) and identically in `EvmErc20V2.sol` (lines 53–63 and 66–77).

The `ExitToNear` precompile (`engine-precompiles/src/native.rs`) can return a non-fatal `ExitError` in several paths:

- `ExitError::OutOfGas` when `required_gas > target_gas` (line 408–410 of `native.rs`)
- `ExitError::Other(...)` for input-validation failures (e.g., invalid flag, invalid recipient encoding)

When SputnikVM processes a precompile returning `Err(ExitError::...)`, the `call()` opcode returns 0 to the calling contract but does **not** revert the calling frame. Because `res` is never tested, `withdrawToNear` returns normally. The OpenZeppelin `_burn` has already committed the balance reduction to EVM storage (`_balances`), but no NEAR promise was ever created. The two accounting systems — EVM ERC-20 balance and NEP-141 balance — are now permanently out of sync.

This is structurally identical to the reported bug: `transfer` writes to one storage location while `transferFrom` reads from another. Here, `_burn` writes to EVM storage while the NEAR-side release (the only path to recover the asset) is silently skipped.

### Impact Explanation
**Critical — Permanent freezing of funds.**

A user who calls `withdrawToNear` or `withdrawToEthereum` under conditions that cause a non-fatal precompile failure will have their EVM mirror tokens burned with no corresponding NEP-141 release. The tokens are unrecoverable: the EVM balance is zero, the NEAR balance is unchanged but inaccessible to the user (it remains locked in the bridge), and there is no re-entry path to retry the NEAR-side transfer.

### Likelihood Explanation
**Medium.** The most realistic trigger is a low-gas transaction: the `ExitToNear` precompile charges `EXIT_TO_NEAR_GAS` upfront. If the remaining gas at the point of the assembly `call()` is below this threshold, the precompile returns `ExitError::OutOfGas`, the `call()` returns 0, and the burn is committed. Any EVM user holding mirror tokens can reach this path by submitting a transaction with a gas limit that is sufficient for `_burn` but insufficient for the precompile. Input-validation failures (malformed recipient bytes) provide a second trigger path.

### Recommendation
Check `res` inside the assembly block and revert on failure in both `withdrawToNear` and `withdrawToEthereum` in both `EvmErc20.sol` and `EvmErc20V2.sol`:

```solidity
assembly {
    let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
    if iszero(res) { revert(0, 0) }
}
```

Alternatively, restructure the functions to call the precompile first and only burn on success.

### Proof of Concept
1. Alice holds 100 units of an `EvmErc20` mirror token on Aurora.
2. Alice calls `withdrawToNear("alice.near", 100)` with a gas limit just sufficient for `_burn` but below `EXIT_TO_NEAR_GAS`.
3. `_burn(alice, 100)` executes successfully; Alice's EVM balance becomes 0.
4. The assembly `call()` to `0xe9217bc70b7ed1f598ddd3199e80b093fa71124f` returns 0 (precompile returns `ExitError::OutOfGas`).
5. `res` is never checked; the function returns without reverting.
6. No NEAR promise is created; the NEP-141 contract never receives a `ft_transfer` call.
7. Alice's 100 tokens are permanently destroyed: EVM balance = 0, NEAR balance unchanged but unreachable. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

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

**File:** engine-precompiles/src/native.rs (L404-410)
```rust
        let required_gas = Self::required_gas(input)?;

        if let Some(target_gas) = target_gas
            && required_gas > target_gas
        {
            return Err(ExitError::OutOfGas);
        }
```

**File:** engine-precompiles/src/lib.rs (L134-157)
```rust
    fn execute(
        &self,
        handle: &mut impl PrecompileHandle,
    ) -> Option<Result<executor::stack::PrecompileOutput, PrecompileFailure>> {
        let address = Address::new(handle.code_address());

        if self.is_paused(&address) {
            return Some(Err(PrecompileFailure::Fatal {
                exit_status: ExitFatal::Other(prelude::Cow::Borrowed("ERR_PAUSED")),
            }));
        }

        let result = match self.all_precompiles.get(&address)? {
            AllPrecompiles::ExitToNear(p) => process_precompile(p, handle),
            AllPrecompiles::ExitToEthereum(p) => process_precompile(p, handle),
            AllPrecompiles::PredecessorAccount(p) => process_precompile(p, handle),
            AllPrecompiles::PrepaidGas(p) => process_precompile(p, handle),
            AllPrecompiles::PromiseResult(p) => process_precompile(p, handle),
            AllPrecompiles::CrossContractCall(p) => process_handle_based_precompile(p, handle),
            AllPrecompiles::Generic(p) => process_precompile(p.as_ref(), handle),
        };

        Some(result.and_then(|output| post_process(output, handle)))
    }
```
