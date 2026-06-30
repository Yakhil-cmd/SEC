### Title
Missing Zero-Address Validation in `withdrawToEthereum` Causes Permanent Fund Loss - (`etc/eth-contracts/contracts/EvmErc20V2.sol`)

---

### Summary

`EvmErc20V2.withdrawToEthereum` burns the caller's tokens unconditionally before invoking the `ExitToEthereum` precompile, and neither the Solidity function nor the precompile validates that `recipient != address(0)`. Passing `address(0)` results in tokens being permanently destroyed on Aurora while the corresponding assets on Ethereum are directed to the uncontrolled zero address.

---

### Finding Description

`EvmErc20V2.withdrawToEthereum` executes in two steps:

1. **Unconditional burn** — `_burn(_msgSender(), amount)` at line 67 destroys the caller's tokens with no precondition on `recipient`.
2. **Precompile call** — the function encodes `recipient` as 20 raw bytes and calls the `ExitToEthereum` precompile at `0xb0bd02f6...`. The assembly `call` return value (`res`) is **never checked**. [1](#0-0) 

Inside the precompile's `run` method (flag `0x1`, ERC-20 path), the 20-byte recipient slice is parsed with `Address::try_from_slice(input)`. This call succeeds for all-zero bytes because `address(0)` is a structurally valid 20-byte value. No zero-address guard exists anywhere in the precompile. [2](#0-1) 

The recipient is then hex-encoded and embedded in the JSON withdrawal args:

```
{"amount": "<n>", "recipient": "0000000000000000000000000000000000000000"}
```

This is forwarded as a `withdraw` promise to the NEP-141 connector. [3](#0-2) 

There is no zero-address rejection at any layer of the Aurora Engine production code path.

---

### Impact Explanation

- Tokens are burned on Aurora (EVM state committed, `totalSupply` decremented).
- The NEAR-side connector processes a withdrawal to `0x0000000000000000000000000000000000000000` on Ethereum — an address no one controls.
- The bridged assets become permanently unclaimable: **critical, permanent freezing/loss of funds**.

The unchecked assembly `res` compounds the issue: even if a future version of the precompile were to reject `address(0)`, the burn would still commit because the revert signal from the inner `call` is silently discarded. [4](#0-3) 

---

### Likelihood Explanation

Any token holder can trigger this with a single direct call. No special privileges, no admin compromise, and no external dependency failure is required. The path is reachable on any deployed `EvmErc20V2` instance. User error (accidental `address(0)`) is a realistic trigger; deliberate griefing of one's own funds is also possible.

---

### Recommendation

1. **Add a zero-address guard in `withdrawToEthereum`**:
   ```solidity
   function withdrawToEthereum(address recipient, uint256 amount) external override {
       require(recipient != address(0), "ERR_ZERO_RECIPIENT");
       _burn(_msgSender(), amount);
       ...
   }
   ```
2. **Check the precompile call return value** and revert if it fails, so the burn is rolled back on any precompile-level error:
   ```solidity
   assembly {
       let res := call(...)
       if iszero(res) { revert(0, 0) }
   }
   ```
3. Optionally add a symmetric guard in the `ExitToEthereum` precompile (`engine-precompiles/src/native.rs`) to reject a zero-byte recipient address at the Rust layer as defense-in-depth.

---

### Proof of Concept

```solidity
// SPDX-License-Identifier: CC0-1.0
pragma solidity ^0.8.0;

interface IEvmErc20V2 {
    function mint(address account, uint256 amount) external;
    function withdrawToEthereum(address recipient, uint256 amount) external;
    function totalSupply() external view returns (uint256);
    function balanceOf(address) external view returns (uint256);
}

contract PoC {
    function exploit(IEvmErc20V2 token) external {
        uint256 amount = 1e18;
        // Precondition: caller holds `amount` tokens (minted by admin in test setup)
        uint256 supplyBefore = token.totalSupply();

        // Step 1: call withdrawToEthereum with address(0) as recipient
        token.withdrawToEthereum(address(0), amount);

        // Step 2: tokens are burned — totalSupply decreased
        assert(token.totalSupply() == supplyBefore - amount);

        // Step 3: no revert occurred; the precompile accepted address(0)
        // The NEAR-side connector will process a withdrawal to 0x0000...0000
        // Those assets are permanently unclaimable on Ethereum.
    }
}
```

Call sequence:
1. Admin mints `1e18` tokens to the attacker address.
2. Attacker calls `withdrawToEthereum(address(0), 1e18)`.
3. `_burn` commits; `totalSupply` drops by `1e18`.
4. Precompile encodes recipient as `"0000000000000000000000000000000000000000"` and schedules a NEAR `withdraw` promise.
5. No revert at any step; tokens are permanently gone.

### Citations

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

**File:** engine-precompiles/src/native.rs (L938-965)
```rust
                if input.len() == 20 {
                    // Parse ethereum address in hex
                    let mut buffer = [0; 40];
                    hex::encode_to_slice(input, &mut buffer).unwrap();
                    let recipient_in_hex = str::from_utf8(&buffer).map_err(|_| {
                        ExitError::Other(Cow::from("ERR_INVALID_RECIPIENT_ADDRESS"))
                    })?;
                    // unwrap cannot fail since we checked the length already
                    let recipient_address = Address::try_from_slice(input)
                        .map_err(|_| ExitError::Other(Cow::from("ERR_WRONG_ADDRESS")))?;

                    (
                        nep141_address,
                        // There is no way to inject json, given the encoding of both arguments
                        // as decimal and hexadecimal respectively.
                        format!(
                            r#"{{"amount": "{}", "recipient": "{}"}}"#,
                            amount.as_u128(),
                            recipient_in_hex
                        )
                        .into_bytes(),
                        events::ExitToEth {
                            sender: Address::new(erc20_address),
                            erc20_address: Address::new(erc20_address),
                            dest: recipient_address,
                            amount,
                        },
                    )
```

**File:** engine-precompiles/src/native.rs (L977-983)
```rust
        let withdraw_promise = PromiseCreateArgs {
            target_account_id: nep141_address,
            method: "withdraw".to_string(),
            args: serialized_args,
            attached_balance: Yocto::new(1),
            attached_gas: costs::WITHDRAWAL_GAS,
        };
```
