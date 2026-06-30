### Title
Unchecked Precompile Call Return Value in `withdrawToNear`/`withdrawToEthereum` Allows Silent Token Burn Without Bridge Transfer - (File: `etc/eth-contracts/contracts/EvmErc20.sol`, `etc/eth-contracts/contracts/EvmErc20V2.sol`)

---

### Summary

`EvmErc20.sol` and `EvmErc20V2.sol` burn the caller's ERC-20 tokens before invoking the Aurora exit precompile via a low-level assembly `call`. The return value of that `call` (stored in `res`) is **never checked**. If the precompile call fails for any reason, the burn is not reverted, the user's tokens are permanently destroyed, and no corresponding NEP-141 transfer is ever scheduled on the NEAR side.

---

### Finding Description

Both `withdrawToNear` and `withdrawToEthereum` in `EvmErc20.sol` follow this pattern:

```solidity
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);                          // tokens destroyed here

    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    uint input_size = 1 + 32 + recipient.length;

    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        // res is NEVER checked — execution continues regardless of success or failure
    }
}
``` [1](#0-0) 

The identical pattern appears in `withdrawToEthereum` and in both functions of `EvmErc20V2.sol`: [2](#0-1) [3](#0-2) [4](#0-3) 

The `ExitToNear` precompile (`0xe9217bc70b7ed1f598ddd3199e80b093fa71124f`) validates its input and returns an `ExitError` on failure (e.g., input too large, invalid recipient format, out of gas). When the precompile fails, the EVM `call` opcode returns `0` into `res`. Because `res` is never inspected and no `require(res != 0)` guard exists, the Solidity function returns normally — after the irreversible `_burn` has already executed.

The precompile's input validation that can trigger failure: [5](#0-4) 

The `recipient` parameter of `withdrawToNear` is a raw `bytes memory` value supplied entirely by the caller. A recipient whose encoded length causes the assembled input to exceed `MAX_INPUT_SIZE`, or whose content fails `parse_recipient`, will cause the precompile to return an error — silently, from the Solidity contract's perspective.

---

### Impact Explanation

When the precompile call fails silently:

1. `_burn` has already reduced the caller's ERC-20 balance to zero — irreversible within the same transaction.
2. No NEAR-side promise is scheduled; the NEP-141 tokens held by the Aurora contract are never released to the user.
3. The user's funds are permanently destroyed with no recovery path.

This is a **Critical — Permanent Freezing / Direct Theft of User Funds** impact. The `error_refund` callback mechanism (`exit_to_near_precompile_callback`) only handles the case where the NEAR promise is scheduled but later fails; it cannot help when the precompile call itself never succeeds and no promise is ever created. [6](#0-5) 

---

### Likelihood Explanation

The `recipient` argument is fully attacker-controlled bytes. A user who accidentally (or deliberately) passes a recipient byte string that is too long, contains invalid UTF-8 for a NEAR account ID, or causes the assembled input to exceed the precompile's `MAX_INPUT_SIZE` will trigger the silent failure. This is a realistic user error path, not a theoretical one, because the function accepts arbitrary `bytes memory` with no on-chain length or format validation before the burn.

---

### Recommendation

Add an explicit check on the assembly `call` return value and revert if the precompile call fails. The burn must either be moved **after** a successful precompile call, or the assembly block must guard with `require`:

```solidity
assembly {
    let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
    if iszero(res) { revert(0, 0) }
}
```

Alternatively, restructure the function to call the precompile first (in a view-like validation step or with a checked call), and only burn tokens after confirming the precompile accepted the input.

---

### Proof of Concept

1. Deploy `EvmErc20` (or use an existing bridged token on Aurora).
2. Mint tokens to `attacker`.
3. Call `withdrawToNear` with a `recipient` byte string whose length causes the assembled input (`1 + 32 + recipient.length`) to exceed the precompile's `MAX_INPUT_SIZE`.
4. Observe: `_burn` executes (attacker's ERC-20 balance drops to zero), the assembly `call` returns `0` (precompile rejected the input), but no revert occurs and no NEAR promise is scheduled.
5. The attacker's ERC-20 tokens are permanently destroyed; the NEP-141 tokens remain locked in the Aurora contract with no recovery mechanism. [1](#0-0) [7](#0-6)

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

**File:** engine-precompiles/src/native.rs (L381-417)
```rust
impl<I: IO> Precompile for ExitToNear<I> {
    fn required_gas(_input: &[u8]) -> Result<EthGas, ExitError> {
        Ok(costs::EXIT_TO_NEAR_GAS)
    }

    #[allow(clippy::too_many_lines)]
    fn run(
        &self,
        input: &[u8],
        target_gas: Option<EthGas>,
        context: &Context,
        is_static: bool,
    ) -> EvmPrecompileResult {
        // ETH (base) transfer input format: (85 bytes)
        //  - flag (1 byte)
        //  - refund_address (20 bytes), present if the feature "error_refund" is enabled
        //  - recipient_account_id (max MAX_INPUT_SIZE - 20 - 1 bytes)
        // ERC-20 transfer input format: (124 bytes)
        //  - flag (1 byte)
        //  - refund_address (20 bytes), present if the feature "error_refund" is enabled.
        //  - amount (32 bytes)
        //  - recipient_account_id (max MAX_INPUT_SIZE - 1 - (20) - 32 bytes)
        //  - `:unwrap` suffix in a case of wNEAR (7 bytes)
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

**File:** engine/src/contract_methods/connector.rs (L231-239)
```rust
        } else if let Some(args) = args.refund {
            // Exit call failed; need to refund tokens
            let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;

            if !refund_result.status.is_ok() {
                return Err(errors::ERR_REFUND_FAILURE.into());
            }

            Some(refund_result)
```
