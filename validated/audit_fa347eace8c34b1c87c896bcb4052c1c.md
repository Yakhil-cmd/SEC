### Title
Unchecked Exit Precompile Call Return Value in ERC-20 Withdrawal Functions Causes Permanent Token Loss - (File: `etc/eth-contracts/contracts/EvmErc20.sol`, `etc/eth-contracts/contracts/EvmErc20V2.sol`)

---

### Summary

Both `EvmErc20::withdrawToNear()` and `EvmErc20::withdrawToEthereum()` (and their `EvmErc20V2` counterparts) burn the caller's ERC-20 tokens first, then invoke the Aurora exit precompile via inline assembly. The return value of the `call` opcode is captured in a local variable `res` but is **never checked**. If the precompile call fails, the EVM `call` returns `0`, the Solidity function does not revert, and the tokens are permanently destroyed with no corresponding NEAR-side transfer — resulting in permanent loss of user funds.

---

### Finding Description

In `EvmErc20.sol` and `EvmErc20V2.sol`, both withdrawal functions follow the same pattern:

**`EvmErc20.sol` — `withdrawToNear` (lines 53–63):**
```solidity
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);   // ← tokens permanently destroyed here

    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    uint input_size = 1 + 32 + recipient.length;

    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        // res is NEVER checked — no `if iszero(res) { revert(0,0) }`
    }
}
```

**`EvmErc20.sol` — `withdrawToEthereum` (lines 65–76):**
```solidity
function withdrawToEthereum(address recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);   // ← tokens permanently destroyed here
    ...
    assembly {
        let res := call(gas(), 0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab, 0, add(input, 32), input_size, 0, 32)
        // res is NEVER checked
    }
}
```

`EvmErc20V2.sol` contains the identical pattern at lines 53–64 and 66–77. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

The `ExitToNear` precompile (`0xe9217bc70b7ed1f598ddd3199e80b093fa71124f`) and `ExitToEthereum` precompile (`0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab`) can return failure (`ExitError`) in multiple conditions documented in `engine-precompiles/src/native.rs`:

- `ERR_TARGET_TOKEN_NOT_FOUND` — ERC-20 not registered in the NEP-141 map
- `ERR_KEY_NOT_FOUND` — eth connector account key missing from storage
- `ERR_INVALID_RECEIVER_ACCOUNT_ID` — malformed recipient
- `ERR_INVALID_AMOUNT` — amount exceeds `u128::MAX`
- `OutOfGas` — insufficient gas forwarded to the precompile
- `ERR_INVALID_IN_DELEGATE` — called via `delegatecall` [5](#0-4) [6](#0-5) 

When any of these conditions occur, the EVM `call` opcode returns `0`. Because `res` is never tested and no `revert` is issued, the outer transaction succeeds. The `_burn` that already executed is not rolled back. The tokens are gone.

The `error_refund` compile-time feature only handles the case where the NEAR-side `ft_transfer` promise fails *after* the precompile successfully scheduled it. It does not protect against the precompile itself returning an error, because in that case no promise is ever scheduled and no callback (`exit_to_near_precompile_callback`) is ever invoked. [7](#0-6) 

---

### Impact Explanation

**Critical — Permanent freezing / direct theft of user funds.**

When the precompile call fails silently:
- The user's ERC-20 tokens on Aurora are permanently burned (supply reduced).
- No NEP-141 tokens are transferred to the NEAR recipient.
- No refund is issued on the EVM side.
- The tokens are irrecoverably lost.

This matches the "Permanent freezing of funds" and "Direct theft of any user funds in motion" impact categories.

---

### Likelihood Explanation

**Low.** Under normal operation the precompile succeeds. However, realistic failure scenarios exist:

1. **Out-of-gas**: A caller who forwards insufficient gas (e.g., sets a low `gas_limit` on the outer EVM transaction) causes the precompile to return `OutOfGas`. The burn still commits.
2. **Unregistered token**: If the ERC-20 contract's address is not present in the NEP-141 map (e.g., a token deployed via a non-standard path), `ERR_TARGET_TOKEN_NOT_FOUND` is returned.
3. **Missing eth connector key**: If the eth connector account storage key is absent, `ERR_KEY_NOT_FOUND` is returned.

Any unprivileged EVM user holding tokens can trigger scenario 1 by simply setting a low gas limit on their transaction.

---

### Recommendation

After the `call` opcode, check `res` and revert if it is zero:

```solidity
assembly {
    let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
    if iszero(res) { revert(0, 0) }
}
```

Apply the same fix to both `withdrawToNear` and `withdrawToEthereum` in both `EvmErc20.sol` and `EvmErc20V2.sol`. This ensures that if the precompile fails, the entire transaction reverts, rolling back the `_burn` and preserving the user's token balance.

---

### Proof of Concept

1. Deploy `EvmErc20` on Aurora with a NEP-141 token that is **not** registered in the engine's NEP-141 map (or use a token whose registration was removed).
2. Mint tokens to `attacker` address.
3. Call `withdrawToNear("victim.near", amount)` from `attacker`.
4. Inside the function, `_burn(attacker, amount)` executes — tokens destroyed.
5. The assembly `call` to the exit precompile returns `0` because `ERR_TARGET_TOKEN_NOT_FOUND` is raised at `get_nep141_from_erc20`.
6. `res` is never checked; no revert occurs.
7. Transaction succeeds. `attacker`'s ERC-20 balance is zero. `victim.near` receives nothing. Tokens are permanently lost.

Alternatively, trigger via out-of-gas: submit the outer EVM transaction with `gas_limit` just enough to execute `_burn` but insufficient for the precompile's `costs::EXIT_TO_NEAR_GAS`, causing `OutOfGas` in the precompile while the burn has already committed. [8](#0-7) [1](#0-0)

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

**File:** engine-precompiles/src/native.rs (L295-320)
```rust
fn validate_input_size(input: &[u8], min: usize, max: usize) -> Result<(), ExitError> {
    if input.len() < min || input.len() > max {
        return Err(ExitError::Other(Cow::from("ERR_INVALID_INPUT")));
    }
    Ok(())
}

fn get_nep141_from_erc20<I: IO>(erc20_token: &[u8], io: &I) -> Result<AccountId, ExitError> {
    AccountId::try_from(
        io.read_storage(bytes_to_key(KeyPrefix::Erc20Nep141Map, erc20_token).as_slice())
            .map(|s| s.to_vec())
            .ok_or(ExitError::Other(Cow::Borrowed(ERR_TARGET_TOKEN_NOT_FOUND)))?,
    )
    .map_err(|_| ExitError::Other(Cow::Borrowed("ERR_INVALID_NEP141_ACCOUNT")))
}

fn get_eth_connector_contract_account<I: IO>(io: &I) -> Result<AccountId, ExitError> {
    io.read_storage(&construct_contract_key(
        EthConnectorStorageId::EthConnectorAccount,
    ))
    .ok_or(ExitError::Other(Cow::Borrowed("ERR_KEY_NOT_FOUND")))
    .and_then(|x| {
        x.to_value()
            .map_err(|_| ExitError::Other(Cow::Borrowed("ERR_DESERIALIZE")))
    })
}
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
