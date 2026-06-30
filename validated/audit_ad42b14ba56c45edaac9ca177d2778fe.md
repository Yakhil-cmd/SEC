### Title
Silent ERC-20 Burn Without Guaranteed NEP-141 Release Causes Permanent Fund Freeze - (File: `etc/eth-contracts/contracts/EvmErc20.sol`)

---

### Summary

In `EvmErc20.sol` and `EvmErc20V2.sol`, the `withdrawToNear` and `withdrawToEthereum` functions unconditionally burn ERC-20 tokens via `_burn` before invoking the exit precompile through an inline assembly `call`. The return value of that assembly `call` is never checked. If the precompile call fails at the EVM level (returns 0), the burn is not reverted, ERC-20 `totalSupply` permanently decreases, and the corresponding NEP-141 tokens remain locked inside Aurora's account with no recovery path. This is the direct analog of the WERC721 `totalSupply`-decreasing-without-releasing-assets pattern.

---

### Finding Description

Both `EvmErc20` and `EvmErc20V2` implement the same exit pattern:

```solidity
// EvmErc20.sol – withdrawToNear (lines 53-63)
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);                          // ← burn is unconditional

    bytes32 amount_b = bytes32(amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    uint input_size = 1 + 32 + recipient.length;

    assembly {
        let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f,
                        0, add(input, 32), input_size, 0, 32)
        // res is NEVER checked
    }
}
``` [1](#0-0) 

The same pattern appears in `withdrawToEthereum` (calling the `ExitToEthereum` precompile at `0xb0bd02f6…`) and in both functions of `EvmErc20V2`: [2](#0-1) [3](#0-2) 

Under standard EVM semantics (and SputnikVM's implementation), a `CALL` to a precompile that returns `Err(ExitError)` causes the inner call to return 0 **without reverting the calling frame**. Because `res` is never inspected, the outer function returns successfully even when the precompile has done nothing.

The `ExitToNear` precompile (`engine-precompiles/src/native.rs`) can return an error in at least the following reachable paths:

1. **`get_nep141_from_erc20` fails** – if the ERC-20 address has no registered NEP-141 mapping (e.g., a contract that implements `IExit` but was not deployed through the official bridge flow).
2. **`parse_amount` rejects the value** – the precompile internally converts the Solidity `uint256` amount to `u128`. The test suite explicitly documents this failure mode:

```rust
#[should_panic(expected = "ERR_INVALID_AMOUNT")]
fn test_exit_with_invalid_amount() {
    let input = (U256::from(u128::MAX) + 1).to_big_endian();
    parse_amount(input.as_slice()).unwrap();
}
``` [4](#0-3) 

A Solidity `uint256` amount can legally exceed `u128::MAX`. If a user holds more than `u128::MAX` ERC-20 units and calls `withdrawToEthereum(recipient, amount)` with that full balance, `_burn` succeeds (Solidity handles `uint256`), the precompile rejects the amount, the assembly `call` silently returns 0, and the tokens are gone.

3. **Asynchronous `ft_transfer` failure** – even when the precompile call succeeds and schedules the NEAR `ft_transfer` promise, that promise can fail if the recipient NEAR account is not registered for storage on the NEP-141 contract. For ERC-20 exits the precompile schedules a plain `ft_transfer` (not `ft_transfer_call`): [5](#0-4) 

There is no callback that re-mints ERC-20 tokens on promise failure for this path (the `exit_to_near_precompile_callback` entrypoint exists for the base-token ETH path, not for ERC-20 exits). The ERC-20 burn is already committed on-chain; the NEP-141 tokens remain locked in Aurora's account.

---

### Impact Explanation

**Critical – Permanent freezing of funds.**

When any of the failure modes above is triggered:
- The ERC-20 `totalSupply` is irreversibly reduced (tokens burned).
- The backing NEP-141 tokens remain locked inside Aurora's account on NEAR.
- There is no on-chain mechanism to recover either asset.

The accounting invariant `erc20.totalSupply() == nep141.balanceOf(aurora)` is permanently broken, exactly mirroring the WERC721 scenario where `totalSupply` decreases while the underlying NFTs stay locked in the wrapper.

---

### Likelihood Explanation

**Medium.** The `u128` overflow path is reachable by any token holder whose balance exceeds `u128::MAX` (possible for tokens with 18 decimals at scale). The asynchronous `ft_transfer` failure path is reachable by any user who provides a recipient NEAR account that has not called `storage_deposit` on the NEP-141 contract — a common mistake for users unfamiliar with NEAR's storage model. No admin privilege or special setup is required; the entry point is the public `withdrawToNear` / `withdrawToEthereum` functions callable by any ERC-20 token holder.

---

### Recommendation

1. **Check the assembly `call` return value and revert on failure:**
   ```solidity
   assembly {
       let res := call(gas(), PRECOMPILE_ADDR, 0, add(input, 32), input_size, 0, 32)
       if iszero(res) { revert(0, 0) }
   }
   ```
   This ensures the burn is atomically rolled back if the precompile rejects the call.

2. **Add a promise callback for ERC-20 exits** analogous to `exit_to_near_precompile_callback` for the base token, so that a failed `ft_transfer` promise triggers a re-mint of the burned ERC-20 tokens.

3. **Validate that `amount` fits in `u128` before burning**, or handle the conversion inside the Solidity contract and revert early.

---

### Proof of Concept

```
1. Alice holds 2^128 units of a bridged ERC-20 token on Aurora.
2. Alice calls EvmErc20.withdrawToEthereum(aliceEthAddr, 2^128).
3. _burn(alice, 2^128) executes successfully — ERC-20 totalSupply drops by 2^128.
4. The assembly call to ExitToEthereum precompile is made.
5. Inside the precompile, parse_amount rejects the value (> u128::MAX) and returns Err(ExitError).
6. The CALL opcode returns 0; `res` is never checked; withdrawToEthereum returns normally.
7. Alice's ERC-20 tokens are permanently burned.
8. The corresponding NEP-141 tokens remain in Aurora's account on NEAR.
9. assert(EvmErc20.totalSupply() < nep141.balanceOf(aurora))  // invariant broken
10. Alice has no recovery path.
```

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

**File:** engine-precompiles/src/native.rs (L627-646)
```rust
        _ => {
            // There is no way to inject json, given the encoding of both arguments
            // as decimal and valid account id respectively.
            (
                nep141_account_id,
                format!(
                    r#"{{"receiver_id":"{}","amount":"{}"}}"#,
                    exit_params.receiver_account_id,
                    exit_params.amount.as_u128()
                ),
                "ft_transfer",
                None,
                events::ExitToNear::Legacy(ExitToNearLegacy {
                    sender: Address::new(erc20_address),
                    erc20_address: Address::new(erc20_address),
                    dest: exit_params.receiver_account_id.to_string(),
                    amount: exit_params.amount,
                }),
            )
        }
```

**File:** engine-precompiles/src/native.rs (L1071-1075)
```rust
    #[should_panic(expected = "ERR_INVALID_AMOUNT")]
    fn test_exit_with_invalid_amount() {
        let input = (U256::from(u128::MAX) + 1).to_big_endian();
        parse_amount(input.as_slice()).unwrap();
    }
```
