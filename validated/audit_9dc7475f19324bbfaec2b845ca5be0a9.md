### Title
Unchecked Precompile Call Return Value in `withdrawToNear`/`withdrawToEthereum` Causes Permanent ERC-20 Token Loss — (File: `etc/eth-contracts/contracts/EvmErc20.sol`, `etc/eth-contracts/contracts/EvmErc20V2.sol`)

---

### Summary

Both `EvmErc20.sol` and `EvmErc20V2.sol` burn a user's ERC-20 tokens before invoking the exit precompile via inline assembly. The return value of that `call` is captured in a local variable (`res`) but is **never checked**. If the precompile call fails (returns `0`), the burn is committed and the transaction succeeds, but no NEAR or Ethereum tokens are ever released. The result is a permanent, irrecoverable loss of the user's bridged assets — a connector/bridge accounting mismatch where the ERC-20 supply decreases without a corresponding release on the other side.

---

### Finding Description

In `EvmErc20.sol`, both `withdrawToNear` and `withdrawToEthereum` follow the same pattern:

```solidity
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);          // ← burn is unconditional

    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    uint input_size = 1 + 32 + recipient.length;

    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f,
                        0, add(input, 32), input_size, 0, 32)
        // res is NEVER checked — no revert on failure
    }
}
``` [1](#0-0) 

The identical pattern appears in `withdrawToEthereum` (precompile `0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab`): [2](#0-1) 

`EvmErc20V2.sol` carries the same defect in both exit functions: [3](#0-2) [4](#0-3) 

When a low-level EVM `call` to a precompile fails, the EVM sets the success word to `0` and returns control to the caller **without reverting the caller's state**. Because `res` is never inspected and no `revert` is issued on `res == 0`, the `_burn` that already executed is final. The user's ERC-20 tokens are destroyed; no NEAR promise or Ethereum withdrawal is ever created.

This is structurally identical to the reported vulnerability class: a fund-movement step (the burn) executes without an on-chain check that the corresponding release step (the precompile call) actually succeeded.

---

### Impact Explanation

**Critical — Permanent freezing/loss of user funds.**

A user who calls `withdrawToNear` or `withdrawToEthereum` when the precompile call fails will have their ERC-20 tokens permanently burned with no corresponding release of NEAR or ETH. There is no recovery path: the burn is committed on-chain, and because no NEAR promise was created, the `exit_to_near_precompile_callback` refund path is never triggered. [5](#0-4) 

The refund callback only fires when a NEAR promise was actually scheduled and then failed. If the precompile call itself returns `0` (i.e., the promise was never created), the callback is never invoked and the tokens are gone.

---

### Likelihood Explanation

**Medium.** The exit precompile can return `0` (failure) under several realistic conditions reachable by an unprivileged user:

1. **Precompile paused**: The exit-to-near precompile checks the contract's paused state. If an admin pauses the precompile for maintenance, any user who calls `withdrawToNear` during that window will burn their tokens with no release. The pause is a legitimate admin operation, not a compromise.

2. **Malformed `recipient` bytes in `withdrawToNear`**: The `recipient` parameter is `bytes memory` — arbitrary caller-supplied bytes. If the precompile validates the NEAR account-ID format and rejects an invalid value, it returns `0`. The burn has already occurred.

3. **Insufficient gas forwarded to the precompile**: Although `gas()` is used, edge cases in gas accounting (e.g., the 63/64 rule applied to nested calls) can leave the precompile with insufficient gas to complete.

Any of these conditions can be triggered by an ordinary token holder without any privileged access.

---

### Recommendation

Revert the transaction if the precompile call fails. Replace the unchecked assembly block with a checked version:

```solidity
assembly {
    let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f,
                    0, add(input, 32), input_size, 0, 32)
    if iszero(res) { revert(0, 0) }
}
```

This ensures that if the precompile call fails for any reason, the entire transaction reverts (including the `_burn`), preserving the user's token balance. Apply the same fix to `withdrawToEthereum` in both `EvmErc20.sol` and `EvmErc20V2.sol`.

---

### Proof of Concept

1. A user holds 1000 units of a bridged ERC-20 token deployed as `EvmErc20`.
2. The exit-to-near precompile is paused by an admin for routine maintenance.
3. The user calls `withdrawToNear(recipient_bytes, 1000)`.
4. `_burn(msg.sender, 1000)` executes — the user's ERC-20 balance drops to 0 and total supply decreases by 1000.
5. The assembly `call` to the paused precompile returns `0` (failure).
6. `res` is never checked; no `revert` is issued; the transaction succeeds.
7. No NEAR promise is created; the `exit_to_near_precompile_callback` is never invoked.
8. The user has lost 1000 tokens permanently: they are burned on the EVM side and never released on the NEAR side.

The same sequence applies to `withdrawToEthereum` (precompile `0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab`) and to `EvmErc20V2.sol`.

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

**File:** etc/eth-contracts/contracts/EvmErc20.sol (L65-77)
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

**File:** etc/eth-contracts/contracts/EvmErc20V2.sol (L66-78)
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
}
```

**File:** engine/src/contract_methods/connector.rs (L196-246)
```rust
pub fn exit_to_near_precompile_callback<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<Option<SubmitResult>, ContractError> {
    with_hashchain(io, env, function_name!(), |io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;
        env.assert_private_call()?;

        // This function should only be called as the callback of
        // exactly one promise.
        if handler.promise_results_count() != 1 {
            return Err(errors::ERR_PROMISE_COUNT.into());
        }

        let args: ExitToNearPrecompileCallbackArgs = io.read_input_borsh()?;

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

        Ok(maybe_result)
    })
}
```
