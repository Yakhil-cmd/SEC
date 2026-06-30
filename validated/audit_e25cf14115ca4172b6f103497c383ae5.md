### Title
Unchecked Precompile Call Return Value in `withdrawToNear`/`withdrawToEthereum` Causes Permanent ERC-20 Token Loss — (`File: etc/eth-contracts/contracts/EvmErc20.sol`)

---

### Summary

`EvmErc20.sol` and `EvmErc20V2.sol` burn ERC-20 tokens before calling the exit precompile via inline assembly, but never check the assembly `call` return value. If the precompile fails for any reason (including an amount exceeding `u128::MAX`, which the precompile rejects), the tokens are permanently destroyed with no corresponding NEP-141 or ETH transfer.

---

### Finding Description

Both `withdrawToNear` and `withdrawToEthereum` in `EvmErc20.sol` follow this pattern:

```solidity
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);          // ← tokens destroyed here

    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    uint input_size = 1 + 32 + recipient.length;

    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f,
                        0, add(input, 32), input_size, 0, 32)
        // res is NEVER checked — no require(res != 0)
    }
}
``` [1](#0-0) 

The identical pattern exists in `EvmErc20V2.sol`: [2](#0-1) 

The `ExitToNear` precompile's `parse_amount` function explicitly rejects any amount exceeding `u128::MAX`:

```rust
fn ft_transfer_call_args(...) -> Result<String, ExitError> {
    if amount > U256::from(u128::MAX) {
        return Err(ExitError::Other(Cow::from("ERR_INVALID_AMOUNT")));
    }
    ...
}
``` [3](#0-2) 

Because ERC-20 balances are `uint256` but NEP-141 balances are `u128`, any user holding more than `u128::MAX` ERC-20 tokens and calling `withdrawToNear(recipient, amount)` with `amount > u128::MAX` will:

1. Have their ERC-20 tokens burned by `_burn` (succeeds — ERC-20 is U256-native).
2. Trigger a precompile call that returns `0` (failure) because `parse_amount` rejects the value.
3. Have the transaction complete successfully because `res` is never checked.
4. Receive zero NEP-141 tokens. The burned ERC-20 tokens are permanently lost.

The same root cause applies to `withdrawToEthereum` in both contracts. [4](#0-3) [5](#0-4) 

The precompile's `run` function also returns `Err` for other reachable conditions (invalid recipient encoding, `ERR_INVALID_IN_STATIC`, `ERR_INVALID_IN_DELEGATE`), all of which produce `res = 0` in the assembly block with no revert. [6](#0-5) 

---

### Impact Explanation

**Critical — Permanent freezing/burning of funds.**

ERC-20 tokens on Aurora represent bridged NEP-141 assets. When `_burn` succeeds but the precompile call silently fails, the ERC-20 supply is reduced without any corresponding NEP-141 transfer. The underlying NEP-141 tokens remain locked in the Aurora contract forever with no mechanism to recover them, because the promise that would release them was never created. This is a permanent, irreversible loss of user funds.

---

### Likelihood Explanation

**Medium.** The `amount > u128::MAX` trigger requires a user to hold an unusually large ERC-20 balance, but:
- ERC-20 minting is unrestricted in amount (U256), so a token with 18 decimals can easily represent values exceeding `u128::MAX` in raw units.
- A user could also trigger this accidentally by passing an invalid recipient string (e.g., one that fails `parse_recipient`), which is a realistic mistake.
- No special privileges are required — any ERC-20 token holder can call `withdrawToNear` or `withdrawToEthereum` directly.

---

### Recommendation

Add a `require` check on the assembly `call` return value in both `withdrawToNear` and `withdrawToEthereum` in both `EvmErc20.sol` and `EvmErc20V2.sol`:

```solidity
assembly {
    let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f,
                    0, add(input, 32), input_size, 0, 32)
    if iszero(res) { revert(0, 0) }
}
```

This ensures that if the precompile rejects the call for any reason, the entire transaction reverts — including the `_burn` — so no tokens are lost.

Additionally, add an explicit Solidity-level guard before the burn:

```solidity
require(amount <= type(uint128).max, "ERR_AMOUNT_TOO_LARGE");
```

---

### Proof of Concept

1. Deploy Aurora with an ERC-20 token backed by a NEP-141 (standard bridge flow).
2. Mint `2**128` ERC-20 tokens to address `alice` (valid because ERC-20 uses `uint256`).
3. Alice calls `withdrawToNear("alice.near", 2**128)`.
4. `_burn(alice, 2**128)` executes successfully — Alice's ERC-20 balance drops to 0.
5. The assembly `call` to the `ExitToNear` precompile (`0xe9217bc70b7ed1f598ddd3199e80b093fa71124f`) returns `0` because `parse_amount` returns `ERR_INVALID_AMOUNT` for values exceeding `u128::MAX`. [7](#0-6) 

6. `res = 0` is never checked; the transaction succeeds.
7. Alice's ERC-20 tokens are gone. No NEP-141 `ft_transfer` promise was created. The NEP-141 tokens remain locked in the Aurora contract with no recovery path.

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

**File:** engine-precompiles/src/native.rs (L758-771)
```rust
            0x1 => {
                let amount = parse_amount(&input[..32])?;
                let Recipient {
                    receiver_account_id,
                    message,
                } = parse_recipient(&input[32..])?;

                Ok(Self::Erc20TokenParams(Erc20TokenParams {
                    #[cfg(feature = "error_refund")]
                    refund_address,
                    receiver_account_id,
                    amount,
                    message,
                }))
```

**File:** engine-precompiles/src/native.rs (L805-807)
```rust
    if amount > U256::from(u128::MAX) {
        return Err(ExitError::Other(Cow::from("ERR_INVALID_AMOUNT")));
    }
```
