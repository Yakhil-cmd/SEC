### Title
ERC-20 Tokens Permanently Burned With No Cross-Chain Transfer Due to Unchecked Precompile Return Value in `withdrawToNear` / `withdrawToEthereum` — (File: `etc/eth-contracts/contracts/EvmErc20.sol`, `etc/eth-contracts/contracts/EvmErc20V2.sol`)

---

### Summary

In `EvmErc20.sol` and `EvmErc20V2.sol`, both `withdrawToNear` and `withdrawToEthereum` burn the caller's ERC-20 tokens **before** invoking the exit precompile via inline assembly. The return value of the assembly `call` is stored in `res` but is **never checked**. If the precompile call fails (returns 0), the EVM sub-call silently fails, the calling function returns successfully, and the user's tokens are permanently destroyed with no corresponding cross-chain transfer initiated.

---

### Finding Description

Both bridge token contracts follow the same pattern:

**`EvmErc20.sol` — `withdrawToNear`** (lines 53–63):
```solidity
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);          // ← tokens destroyed here

    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    uint input_size = 1 + 32 + recipient.length;

    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        // res is NEVER checked — silent failure
    }
}
```

**`EvmErc20.sol` — `withdrawToEthereum`** (lines 65–76) and both functions in `EvmErc20V2.sol` (lines 53–77) exhibit the identical pattern.

The precompile at `0xe9217bc70b7ed1f598ddd3199e80b093fa71124f` is `ExitToNear`. Its `run` method in `engine-precompiles/src/native.rs` can return a non-fatal `ExitError` in several reachable paths:

1. **Invalid NEAR recipient account ID** — `parse_recipient` validates the bytes and returns `Err(ExitError::Other("ERR_INVALID_RECIPIENT_ACCOUNT_ID"))` for any malformed input.
2. **ERC-20 not registered in NEP-141 map** — `get_nep141_from_erc20` returns `Err(ExitError::Other("Target token not found"))` if the token is not registered.
3. **Invalid amount** — `parse_amount` returns `Err(ExitError::Other("ERR_INVALID_AMOUNT"))` for amounts exceeding `u128::MAX`.

In SputnikVM (Aurora's EVM), a non-fatal `ExitError` from a precompile causes the sub-`call` to return `res = 0` **without reverting the calling context**. Because neither `EvmErc20` nor `EvmErc20V2` checks `res`, the `withdraw*` function returns successfully. The `_burn` is committed, but no promise log is emitted, so no NEAR-side `ft_transfer` or `withdraw` is ever scheduled.

The `ExitToEthereum` precompile at `0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab` is subject to the same unchecked-return pattern in `withdrawToEthereum`.

---

### Impact Explanation

**Critical — Permanent freezing/burning of user funds.**

When the precompile call fails silently:
- The user's ERC-20 balance is reduced by `amount` (tokens burned).
- No NEAR-side promise is scheduled; the NEP-141 `ft_transfer` never executes.
- There is no recovery path: the ERC-20 tokens are gone, and no NEP-141 tokens arrive at the recipient.
- The total bridged supply becomes permanently insolvent (EVM supply decreases, NEAR supply unchanged).

This matches the exact impact class of the reference report: tokens destroyed on one side with no corresponding credit on the other side, and no recovery mechanism.

---

### Likelihood Explanation

**Medium-High.** The `withdrawToNear` function accepts arbitrary `bytes memory recipient`. Any user who:
- Provides a NEAR account ID that fails `parse_recipient` validation (e.g., contains `@`, uppercase letters, or is otherwise malformed per NEAR account ID rules), or
- Calls the function on an ERC-20 that is not yet registered in the NEP-141 map (e.g., a custom contract inheriting `EvmErc20` deployed outside the factory),

will silently lose their tokens. The `withdrawToEthereum` path is less likely to trigger this via user input (Ethereum addresses are always 20 bytes and structurally valid), but the `get_nep141_from_erc20` lookup can still fail.

---

### Recommendation

1. **Check the precompile return value and revert on failure** — the minimal fix:

```solidity
assembly {
    let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
    if iszero(res) { revert(0, 0) }
}
```

2. **Prefer check-then-act ordering** — call the precompile first (as a view/static call or with a revert guard), and only burn tokens after confirming the precompile will succeed. This eliminates the burn-before-validate race entirely.

3. **Validate the recipient on-chain** before burning, where feasible (e.g., minimum length, character set).

---

### Proof of Concept

**Scenario: Invalid NEAR recipient causes silent token loss**

1. Alice holds 100 units of a bridged ERC-20 token (`EvmErc20` instance, properly registered).
2. Alice calls `withdrawToNear("INVALID ACCOUNT!", 100)` — the recipient contains spaces and uppercase letters, which are invalid NEAR account ID characters.
3. Inside `withdrawToNear`:
   - `_burn(Alice, 100)` executes — Alice's balance drops to 0.
   - The assembly `call` invokes `ExitToNear`.
   - `ExitToNear::run` calls `ExitToNearParams::try_from(input)`, which calls `parse_recipient`, which calls `AccountId::try_from("INVALID ACCOUNT!")` — this fails, returning `Err(ExitError::Other("ERR_INVALID_RECIPIENT_ACCOUNT_ID"))`.
   - The precompile returns a non-fatal error; the sub-call returns `res = 0`.
   - `res` is not checked; `withdrawToNear` returns normally.
4. Alice's ERC-20 balance is 0. No `ft_transfer` promise was scheduled. No NEP-141 tokens arrive. Funds are permanently lost.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

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

**File:** engine-precompiles/src/native.rs (L419-447)
```rust
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

**File:** engine-precompiles/src/native.rs (L470-483)
```rust
        let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
            PromiseArgs::Create(transfer_promise)
        } else {
            PromiseArgs::Callback(PromiseWithCallbackArgs {
                base: transfer_promise,
                callback: PromiseCreateArgs {
                    target_account_id: self.current_account_id.clone(),
                    method: "exit_to_near_precompile_callback".to_string(),
                    args: borsh::to_vec(&callback_args).unwrap(),
                    attached_balance: Yocto::new(0),
                    attached_gas: costs::EXIT_TO_NEAR_CALLBACK_GAS,
                },
            })
        };
```

**File:** engine-precompiles/src/native.rs (L582-583)
```rust
    let erc20_address = context.caller; // because ERC-20 contract calls the precompile.
    let nep141_account_id = get_nep141_from_erc20(erc20_address.as_bytes(), io)?;
```
