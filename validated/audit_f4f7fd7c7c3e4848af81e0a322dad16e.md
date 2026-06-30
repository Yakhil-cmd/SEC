### Title
Unchecked Exit Precompile Return Value Causes Permanent ERC-20 Token Loss on Failed Withdrawal - (File: `etc/eth-contracts/contracts/EvmErc20.sol`, `etc/eth-contracts/contracts/EvmErc20V2.sol`)

---

### Summary

Both `EvmErc20.sol` and `EvmErc20V2.sol` burn the caller's ERC-20 tokens **before** invoking the exit precompile, and then **never check the return value** of that precompile `call`. If the precompile call fails for any reason (e.g., the exit precompile is paused, the input is rejected, or gas is exhausted), the burn is not reverted, the user's tokens are permanently destroyed, and no corresponding NEAR or Ethereum tokens are ever received.

---

### Finding Description

In `EvmErc20.sol`, both `withdrawToNear` and `withdrawToEthereum` follow this pattern:

```solidity
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);          // tokens destroyed here

    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    uint input_size = 1 + 32 + recipient.length;

    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        // res is NEVER checked — no require, no revert
    }
}
``` [1](#0-0) 

`EvmErc20V2.sol` has the identical structural flaw in both `withdrawToNear` and `withdrawToEthereum`: [2](#0-1) [3](#0-2) 

The exit precompile (`ExitToNear`) returns `Err(ExitError::...)` in multiple reachable conditions — for example, when the precompile is paused, when the input fails parsing (`ExitToNearParams::try_from(input)?`), or when called in an invalid context: [4](#0-3) 

When the precompile returns an `ExitError`, the EVM `call` opcode returns `0` (failure) **without reverting the calling frame**. Because `EvmErc20.sol` does not check `res`, the `_burn` that already executed is not rolled back. The user's ERC-20 tokens are gone, and no NEAR-side `ft_transfer` promise is ever scheduled.

The `error_refund` callback mechanism (`exit_to_near_precompile_callback`) only handles the case where the precompile call **succeeds** at the EVM level but the downstream NEAR promise fails — it provides no protection when the precompile call itself returns `0`: [5](#0-4) 

---

### Impact Explanation

**Critical — Permanent freezing of funds.**

When the exit precompile call fails silently:
- The user's ERC-20 tokens are burned and permanently destroyed.
- No NEAR-side `ft_transfer` or `ft_transfer_call` is ever scheduled.
- There is no refund path: the `error_refund` callback is only triggered by a failed NEAR promise, not by a failed EVM-level precompile call.
- The total supply of the bridged ERC-20 decreases while the NEP-141 balance held by Aurora remains unchanged, creating a permanent accounting discrepancy and irrecoverable loss for the user.

---

### Likelihood Explanation

The most concrete trigger is the **exit precompile being paused**. Aurora has an explicit `pause_precompiles` / `resume_precompiles` mechanism for the exit precompiles, used for maintenance or emergency response. During any such pause, every call to `withdrawToNear` or `withdrawToEthereum` on any deployed `EvmErc20` / `EvmErc20V2` contract will silently burn the caller's tokens with no recourse. This is a legitimate operational state, not an admin compromise. Any user who calls the function during a pause loses funds permanently.

A secondary trigger is a malformed or oversized `recipient` argument causing `ExitToNearParams::try_from(input)` to return an error inside the precompile, which similarly causes the precompile call to return `0` without reverting the burn. [6](#0-5) 

---

### Recommendation

Check the return value of the precompile `call` in both functions and revert if it is `0`:

```solidity
assembly {
    let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
    if iszero(res) { revert(0, 0) }
}
```

This must be applied to all four affected assembly blocks across `EvmErc20.sol` and `EvmErc20V2.sol` (`withdrawToNear` and `withdrawToEthereum` in each). This ensures that if the exit precompile rejects the call for any reason, the entire transaction reverts, the `_burn` is rolled back, and the user retains their tokens. [7](#0-6) [8](#0-7) 

---

### Proof of Concept

1. Deploy `EvmErc20` for a bridged NEP-141 token. Mint tokens to `user`.
2. Admin calls `pause_precompiles` to pause the `exitToNear` precompile (legitimate maintenance operation).
3. `user` calls `withdrawToNear(recipient, amount)`.
4. `_burn(user, amount)` executes — user's ERC-20 balance is reduced to zero.
5. The `call` to `0xe9217bc70b7ed1f598ddd3199e80b093fa71124f` returns `0` (precompile is paused, returns `ExitError`).
6. `res` is never checked; the function returns successfully.
7. No NEAR-side `ft_transfer` is scheduled. The user has lost `amount` tokens permanently.
8. The NEP-141 balance held by Aurora is unchanged; the ERC-20 total supply has decreased by `amount` — a permanent bridge accounting discrepancy.

### Citations

**File:** etc/eth-contracts/contracts/EvmErc20.sol (L53-77)
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
}
```

**File:** etc/eth-contracts/contracts/EvmErc20V2.sol (L53-77)
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

**File:** engine-precompiles/src/native.rs (L381-420)
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

        let exit_to_near_params = ExitToNearParams::try_from(input)?;

```

**File:** engine/src/contract_methods/connector.rs (L214-242)
```rust
        let maybe_result = if let Some(PromiseResult::Successful(_)) = handler.promise_result(0) {
            if let Some(args) = args.transfer_near {
                let action = PromiseAction::Transfer {
                    amount: Yocto::new(args.amount),
                };
                let promise = PromiseBatchAction {
                    target_account_id: args.target_account_id,
                    actions: vec![action],
                };

                // Safety: this call is safe because it comes from the exit to near precompile, not users.
                // The call is to transfer the unwrapped wNEAR tokens.
                let promise_id = handler.promise_create_batch(&promise);
                handler.promise_return(promise_id);
            }

            None
        } else if let Some(args) = args.refund {
            // Exit call failed; need to refund tokens
            let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;

            if !refund_result.status.is_ok() {
                return Err(errors::ERR_REFUND_FAILURE.into());
            }

            Some(refund_result)
        } else {
            None
        };
```
