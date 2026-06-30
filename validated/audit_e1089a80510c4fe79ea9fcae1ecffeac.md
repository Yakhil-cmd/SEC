### Title
Missing Zero-Address Check in `ExitToEthereum` Precompile Allows Permanent Fund Loss - (File: `engine-precompiles/src/native.rs`)

### Summary
The `ExitToEthereum` precompile and the `EvmErc20`/`EvmErc20V2` Solidity contracts accept `address(0)` as a withdrawal recipient without validation. Any token holder can burn their bridged ERC-20 tokens on Aurora and dispatch a withdrawal promise to the ETH connector targeting the zero Ethereum address, resulting in permanent, irrecoverable fund loss.

### Finding Description
In `etc/eth-contracts/contracts/EvmErc20.sol` and `EvmErc20V2.sol`, the `withdrawToEthereum` function accepts an arbitrary `address recipient` parameter with no zero-address guard: [1](#0-0) 

The function immediately burns the caller's tokens and then encodes `recipient` as raw bytes into the precompile calldata: [1](#0-0) [2](#0-1) 

The encoded calldata is forwarded to the `ExitToEthereum` precompile at `0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab`. Inside the precompile's `run` method (flag `0x01`, ERC-20 path), the recipient address is parsed from the raw 20-byte input with no zero-address check: [3](#0-2) 

The parsed `recipient_address` (which may be all-zeros) is then embedded directly into the serialized withdrawal arguments sent to the ETH connector: [4](#0-3) 

A `PromiseCreateArgs` targeting the ETH connector's `withdraw` method is constructed and emitted as a log, with the zero Ethereum address as the withdrawal destination: [5](#0-4) 

The same missing check exists for the ETH base-token path (flag `0x00`): [6](#0-5) 

The `AdminControlled.sol` constructor also explicitly suppresses the slither zero-check warning, acknowledging the pattern exists in the codebase: [7](#0-6) 

### Impact Explanation
The burn in `_burn(_msgSender(), amount)` is irreversible. Once executed, the ERC-20 tokens are destroyed on Aurora. The subsequent withdrawal promise to the ETH connector carries `0x0000000000000000000000000000000000000000` as the Ethereum recipient. Whether the ETH connector rejects or processes this withdrawal, the Aurora-side tokens are already gone. The result is **permanent, irrecoverable loss of the user's bridged funds** — matching the Critical impact tier (permanent freezing/loss of funds).

### Likelihood Explanation
The entry path is fully unprivileged: any holder of a bridged ERC-20 token on Aurora can call `withdrawToEthereum(address(0), amount)` directly. A realistic trigger is a smart contract that calls `withdrawToEthereum` with an uninitialized or incorrectly computed recipient variable (a common Solidity footgun). No admin access, key compromise, or governance action is required.

### Recommendation
1. In `EvmErc20.sol` and `EvmErc20V2.sol`, add a guard at the top of `withdrawToEthereum`:
   ```solidity
   require(recipient != address(0), "ERR_ZERO_RECIPIENT");
   ```
2. In `engine-precompiles/src/native.rs`, inside `ExitToEthereum::run`, after parsing `recipient_address` for both the `0x00` (ETH) and `0x01` (ERC-20) paths, add:
   ```rust
   if recipient_address == Address::zero() {
       return Err(ExitError::Other(Cow::from("ERR_ZERO_RECIPIENT_ADDRESS")));
   }
   ```
   This defense-in-depth check at the precompile layer protects against any caller, not just `EvmErc20`.

### Proof of Concept
1. Deploy or use an existing bridged ERC-20 (`EvmErc20`) on Aurora mainnet.
2. As a token holder, call:
   ```solidity
   evmErc20.withdrawToEthereum(address(0), 1_000_000);
   ```
3. Observe: `_burn` destroys `1_000_000` tokens from the caller's balance.
4. The `ExitToEthereum` precompile encodes `0x0000...0000` as the Ethereum recipient and emits a `PromiseCreateArgs` log targeting the ETH connector's `withdraw` method.
5. The ETH connector receives a withdrawal request for the zero Ethereum address; the Aurora-side tokens are already burned and cannot be recovered regardless of the connector's behavior.
6. Funds are permanently lost.

### Citations

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

**File:** engine-precompiles/src/native.rs (L894-896)
```rust
                let recipient_address: Address = input
                    .try_into()
                    .map_err(|_| ExitError::Other(Cow::from("ERR_INVALID_RECIPIENT_ADDRESS")))?;
```

**File:** engine-precompiles/src/native.rs (L946-947)
```rust
                    let recipient_address = Address::try_from_slice(input)
                        .map_err(|_| ExitError::Other(Cow::from("ERR_WRONG_ADDRESS")))?;
```

**File:** engine-precompiles/src/native.rs (L949-965)
```rust
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

**File:** engine-precompiles/src/native.rs (L977-990)
```rust
        let withdraw_promise = PromiseCreateArgs {
            target_account_id: nep141_address,
            method: "withdraw".to_string(),
            args: serialized_args,
            attached_balance: Yocto::new(1),
            attached_gas: costs::WITHDRAWAL_GAS,
        };

        let promise = borsh::to_vec(&PromiseArgs::Create(withdraw_promise)).unwrap();
        let promise_log = Log {
            address: exit_to_ethereum::ADDRESS.raw(),
            topics: Vec::new(),
            data: promise,
        };
```

**File:** etc/eth-contracts/contracts/AdminControlled.sol (L10-13)
```text
    constructor(address _admin, uint flags) {
        // slither-disable-next-line missing-zero-check
        admin = _admin;

```
