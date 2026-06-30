### Title
`EvmErc20` Burns Tokens Before Checking Exit Precompile Return Value, Causing Permanent Fund Loss - (`etc/eth-contracts/contracts/EvmErc20.sol`, `etc/eth-contracts/contracts/EvmErc20V2.sol`)

### Summary

`EvmErc20.sol` and `EvmErc20V2.sol` call `_burn` before invoking the exit precompile via inline assembly, and never check the return value of that precompile call. If the precompile call fails (returns 0), the user's ERC-20 tokens are permanently destroyed on the Aurora/EVM side while the corresponding NEP-141 tokens on NEAR are never released.

### Finding Description

In both `EvmErc20.sol` and `EvmErc20V2.sol`, the `withdrawToNear` and `withdrawToEthereum` functions follow this pattern:

1. Call `_burn(_msgSender(), amount)` — this irreversibly destroys the user's ERC-20 tokens and emits a `Transfer(sender, address(0), amount)` event.
2. Encode calldata for the exit precompile.
3. Invoke the precompile via inline assembly `call(...)`, storing the return value in `res`.
4. **Never check `res`** — execution always continues regardless of whether the precompile succeeded or failed.

```solidity
// EvmErc20.sol lines 53-63
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);          // tokens destroyed here unconditionally

    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    uint input_size = 1 + 32 + recipient.length;

    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        // res is captured but NEVER checked
    }
}
```

The same pattern appears in `withdrawToEthereum` in both contracts.

The exit precompile (`ExitToNear` / `ExitToEthereum`) can legitimately return failure (`ExitError`) in several cases defined in `engine-precompiles/src/native.rs`:
- The NEP-141 ↔ ERC-20 mapping does not exist for the calling token (`get_nep141_from_erc20` fails with `ERR_TARGET_TOKEN_NOT_FOUND`)
- The precompile is called in static/delegate context
- Input parsing fails
- The eth-connector account lookup fails

When any of these conditions occur, the EVM `call` returns 0, but because `res` is never checked, the Solidity function returns normally. The `_burn` has already executed: the user's balance is zero, the `Transfer` event to `address(0)` has been emitted, and no NEAR-side promise is scheduled.

### Impact Explanation

**Critical — Permanent freezing of funds.**

Any user who calls `withdrawToNear` or `withdrawToEthereum` on an `EvmErc20`/`EvmErc20V2` token when the exit precompile fails will have their tokens permanently destroyed with no recourse. The ERC-20 supply is reduced, the user's balance is zero, but the NEP-141 tokens held by the Aurora contract on NEAR are never released. The funds are irrecoverably lost.

### Likelihood Explanation

**Medium.** The most realistic trigger is a token whose NEP-141 ↔ ERC-20 mapping is absent or stale (e.g., a token deployed via `deploy_erc20_token` whose mapping was never written, or a token on a silo instance where the mapping lookup fails). Any token holder interacting with such a token triggers the loss. No special privileges are required — any ERC-20 holder can call `withdrawToNear` or `withdrawToEthereum` directly.

### Recommendation

Check the return value of the precompile `call` and revert if it fails. The `_burn` should only execute after confirming the precompile call succeeded, or the entire operation should be atomic (burn only on success):

```solidity
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    // ... encode input ...
    bool success;
    assembly {
        success := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
    }
    require(success, "ERR_EXIT_PRECOMPILE_FAILED");
    _burn(_msgSender(), amount);  // burn only after confirmed success
}
```

Alternatively, burn after the precompile call so that a failed precompile call leaves the user's balance intact.

### Proof of Concept

1. Deploy an `EvmErc20` token whose NEP-141 mapping is not registered in Aurora's storage (so `get_nep141_from_erc20` returns `ERR_TARGET_TOKEN_NOT_FOUND`).
2. Mint tokens to `alice`.
3. `alice` calls `withdrawToNear(recipient, amount)`.
4. `_burn(alice, amount)` executes: alice's balance drops to 0, `Transfer(alice, 0x0, amount)` is emitted.
5. The precompile call returns 0 (failure) because the NEP-141 mapping is absent.
6. `res` is never checked; the function returns normally.
7. Alice's ERC-20 tokens are gone. No NEP-141 is released on NEAR. Funds are permanently lost.

The root cause is at: [1](#0-0) [2](#0-1) 

The precompile failure paths that trigger this are in: [3](#0-2)

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

**File:** engine-precompiles/src/native.rs (L412-447)
```rust
        // It's not allowed to call exit precompiles in static mode
        if is_static {
            return Err(ExitError::Other(Cow::from("ERR_INVALID_IN_STATIC")));
        } else if context.address != exit_to_near::ADDRESS.raw() {
            return Err(ExitError::Other(Cow::from("ERR_INVALID_IN_DELEGATE")));
        }

        let exit_to_near_params = ExitToNearParams::try_from(input)?;

        let (nep141_address, args, exit_event, method, transfer_near_args) =
            match exit_to_near_params {
                // ETH(base) token transfer
                //
                // Input slice format:
                //  recipient_account_id (bytes) - the NEAR recipient account which will receive
                //  NEP-141 (base) tokens, or also can contain the `:unwrap` suffix in case of
                //  withdrawing wNEAR, or another message of JSON in case of OMNI, or address of
                //  receiver in case of transfer tokens to another engine contract.
                ExitToNearParams::BaseToken(ref exit_params) => {
                    let eth_connector_account_id = self.get_eth_connector_contract_account()?;
                    exit_base_token_to_near(eth_connector_account_id, context, exit_params)?
                }
                // ERC-20 token transfer
                //
                // This precompile branch is expected to be called from the ERC-20 burn function.
                //
                // Input slice format:
                //  amount (U256 big-endian bytes) - the amount that was burned
                //  recipient_account_id (bytes) - the NEAR recipient account which will receive
                //  NEP-141 tokens, or also can contain the `:unwrap` suffix in case of withdrawing
                //  wNEAR, or another message of JSON in case of OMNI, or address of receiver in case
                //  of transfer tokens to another engine contract.
                ExitToNearParams::Erc20TokenParams(ref exit_params) => {
                    exit_erc20_token_to_near(context, exit_params, &self.io)?
                }
            };
```
