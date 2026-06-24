### Title
Missing Zero-Principal Validation in ckETH/ckERC20 Deposit Helper Contracts Causes Permanent Fund Loss - (File: rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol)

### Summary
The `depositEth` and `depositErc20` functions in the ckETH/ckERC20 Ethereum-side helper contracts do not validate that the `principal` parameter is non-zero (`bytes32(0)`). When a user passes a zero principal, ETH or ERC20 tokens are irrevocably transferred to the minter's Ethereum address, but the IC minter rejects the deposit event as invalid, permanently locking the funds with no recovery path.

### Finding Description
Three production Solidity helper contracts in the IC repository accept an arbitrary `bytes32 principal` argument without checking it is non-zero:

**`DepositHelperWithSubaccount.sol` — `depositEth`** (line 503):
```solidity
function depositEth(bytes32 principal, bytes32 subaccount) public payable {
    emit ReceivedEthOrErc20(ZERO_ADDRESS, msg.sender, msg.value, principal, subaccount);
    minterAddress.transfer(msg.value);
}
```
No check on `principal`. [1](#0-0) 

**`DepositHelperWithSubaccount.sol` — `depositErc20`** (line 511): checks `erc20Address != ZERO_ADDRESS` but not `principal`. [2](#0-1) 

**`EthDepositHelper.sol` — `deposit`** (line 32): accepts any `bytes32 _principal` with no validation. [3](#0-2) 

**`ERC20DepositHelper.sol` — `deposit`** (line 498): accepts any `bytes32 principal` with no validation. [4](#0-3) 

On the IC side, the minter's `parse_principal_from_slice` function explicitly rejects a zero `bytes32` (which encodes `num_bytes == 0`) with the error `"management canister principal is not allowed"`:

```rust
let num_bytes = slice[0] as usize;
if num_bytes == 0 {
    return Err("management canister principal is not allowed".to_string());
}
``` [5](#0-4) 

The minter then records the event as `InvalidDeposit` via `register_deposit_events`, permanently discarding it: [6](#0-5) 

There is no recovery mechanism for funds deposited with an invalid principal. The minter's Ethereum address holds the ETH/ERC20 indefinitely.

### Impact Explanation
A user who passes `bytes32(0)` as the `principal` argument (due to a bug in a frontend, a scripting error, or a malicious dApp) will:
1. Have their ETH or ERC20 tokens transferred to the minter's Ethereum address (irreversible on-chain).
2. Receive no ckETH/ckERC20 in return, because the IC minter marks the event `InvalidDeposit`.
3. Have no recourse — the IC minter has no admin function to re-process or refund invalid deposits.

This constitutes **permanent, unrecoverable loss of user funds** in the chain-fusion deposit path.

### Likelihood Explanation
- Any user, frontend, or script that passes `bytes32(0)` as the principal triggers this. The documentation warns that "it's critical that the encoded IC principal is correct otherwise the funds will be lost," but the contract itself provides no on-chain guard. [7](#0-6) 
- A compromised or buggy frontend could silently pass zero principals for all deposits, causing systematic fund loss across many users.
- Likelihood is **Low-Medium**: individual user error is plausible; a malicious frontend targeting this is a realistic attack vector.

### Recommendation
Add a zero-principal guard to every deposit function in all three helper contracts:

```solidity
require(principal != bytes32(0), "CkDeposit: zero principal not allowed");
```

This mirrors the existing `erc20Address` zero-check already present in `depositErc20` and prevents funds from being sent to the minter with an unroutable destination.

### Proof of Concept
1. Attacker or buggy frontend calls `depositEth{value: 1 ether}(bytes32(0), bytes32(0))` on `DepositHelperWithSubaccount`.
2. Contract emits `ReceivedEthOrErc20(address(0), msg.sender, 1 ether, bytes32(0), bytes32(0))` and transfers 1 ETH to `minterAddress`. [1](#0-0) 
3. IC minter scrapes the log via `scrape_logs` → `ReceivedEthOrErc20LogParser::parse_log` → `parse_principal(&entry.topics[3], event_source)`. [8](#0-7) 
4. `parse_principal_from_slice([0u8; 32])` reads `num_bytes = 0` → returns `Err("management canister principal is not allowed")`. [5](#0-4) 
5. `register_deposit_events` records `EventType::InvalidDeposit` for the event source. [9](#0-8) 
6. 1 ETH is permanently locked in the minter's Ethereum address. No ckETH is minted. No refund is possible.

### Citations

**File:** rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol (L503-506)
```text
    function depositEth(bytes32 principal, bytes32 subaccount) public payable {
        emit ReceivedEthOrErc20(ZERO_ADDRESS, msg.sender, msg.value, principal, subaccount);
        minterAddress.transfer(msg.value);
    }
```

**File:** rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol (L511-532)
```text
    function depositErc20(
        address erc20Address,
        uint256 amount,
        bytes32 principal,
        bytes32 subaccount
    ) public {
        require(erc20Address != ZERO_ADDRESS, "ERC20: depositErc20 from the zero address");
        IERC20 erc20Token = IERC20(erc20Address);
        erc20Token.safeTransferFrom(
            msg.sender,
            minterAddress,
            amount
        );

        emit ReceivedEthOrErc20(
            erc20Address,
            msg.sender,
            amount,
            principal,
            subaccount
        );
    }
```

**File:** rs/ethereum/cketh/minter/EthDepositHelper.sol (L32-35)
```text
    function deposit(bytes32 _principal) public payable {
        emit ReceivedEth(msg.sender, msg.value, _principal);
        cketh_minter_main_address.transfer(msg.value);
    }
```

**File:** rs/ethereum/cketh/minter/ERC20DepositHelper.sol (L498-503)
```text
    function deposit(address erc20_address, uint256 amount, bytes32 principal) public {
        IERC20 erc20Token = IERC20(erc20_address);
        erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);

        emit ReceivedErc20(erc20_address, msg.sender, amount, principal);
    }
```

**File:** rs/ethereum/cketh/minter/src/eth_logs/mod.rs (L269-272)
```rust
    let num_bytes = slice[0] as usize;
    if num_bytes == 0 {
        return Err("management canister principal is not allowed".to_string());
    }
```

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L330-341)
```rust
            mutate_state(|s| {
                process_event(
                    s,
                    EventType::InvalidDeposit {
                        event_source: event.source(),
                        reason: format!("blocked address {}", event.from_address()),
                    },
                )
            });
        } else {
            mutate_state(|s| process_event(s, event.into_deposit()));
        }
```

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L347-358)
```rust
        if let ReceivedEventError::InvalidEventSource { source, error } = &error {
            mutate_state(|s| {
                process_event(
                    s,
                    EventType::InvalidDeposit {
                        event_source: *source,
                        reason: error.to_string(),
                    },
                )
            });
        }
        report_transaction_error(error);
```

**File:** rs/ethereum/cketh/docs/cketh.adoc (L115-119)
```text
[WARNING]
====
* It's critical that the encoded IC principal is correct otherwise the funds will be lost.
* The helper smart contracts for Ethereum and for Sepolia have different addresses (refer to the above table).
====
```

**File:** rs/ethereum/cketh/minter/src/eth_logs/parser.rs (L126-126)
```rust
        let principal = parse_principal(&entry.topics[3], event_source)?;
```
