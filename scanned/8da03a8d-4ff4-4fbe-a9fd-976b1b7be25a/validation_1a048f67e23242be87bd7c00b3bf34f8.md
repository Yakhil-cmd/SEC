### Title
No Zero-Address Validation of `minterAddress` Constructor Parameter in ckETH/ckERC20 Deposit Helper Contracts - (`File: rs/ethereum/cketh/minter/EthDepositHelper.sol`, `rs/ethereum/cketh/minter/ERC20DepositHelper.sol`, `rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol`)

---

### Summary

Three production Ethereum helper contracts that form the on-chain deposit layer of the IC's ckETH/ckERC20 chain-fusion bridge each accept a minter address as a constructor parameter and assign it to an `immutable` state variable without checking whether the supplied value is `address(0)`. Because the variable is `immutable`, there is no setter and no upgrade path; a zero-address deployment permanently breaks the contract and causes all subsequent user deposits to be lost or permanently stuck.

---

### Finding Description

**`CkEthDeposit` (`EthDepositHelper.sol`)**

```solidity
address payable private immutable cketh_minter_main_address;

constructor(address _cketh_minter_main_address) {
    cketh_minter_main_address = payable(_cketh_minter_main_address); // no zero-check
}
```

`deposit()` unconditionally calls `cketh_minter_main_address.transfer(msg.value)`. If the address is `address(0)`, the ETH transfer reverts (no code at address 0 in EVM), permanently bricking the contract. [1](#0-0) 

---

**`CkErc20Deposit` (`ERC20DepositHelper.sol`)**

```solidity
address private immutable cketh_minter_main_address;

constructor(address _cketh_minter_main_address) {
    cketh_minter_main_address = _cketh_minter_main_address; // no zero-check
}
```

`deposit()` calls `safeTransferFrom(msg.sender, cketh_minter_main_address, amount)`. If the address is `address(0)`, the ERC-20 transfer reverts (standard ERC-20 rejects zero-address recipient), permanently bricking the contract. [2](#0-1) 

---

**`CkDeposit` (`DepositHelperWithSubaccount.sol`) — most egregious instance**

```solidity
address constant private ZERO_ADDRESS = address(0);   // defined but not used in constructor
address payable private immutable minterAddress;

constructor(address _minterAddress) {
    minterAddress = payable(_minterAddress);           // no zero-check despite ZERO_ADDRESS existing
}
```

The contract already defines `ZERO_ADDRESS` and uses it to guard `erc20Address` inside `depositErc20`:

```solidity
require(erc20Address != ZERO_ADDRESS, "ERC20: depositErc20 from the zero address");
```

Yet the identical guard is absent for `_minterAddress` in the constructor. This is an internal inconsistency: the developer was aware of the zero-address risk for token addresses but did not apply the same discipline to the minter address. [3](#0-2) [4](#0-3) 

---

### Impact Explanation

If any of these contracts is deployed with `_minterAddress = address(0)`:

- **`CkEthDeposit`**: every call to `deposit()` reverts; all ETH sent by users is returned but the contract is permanently non-functional. The IC minter canister never receives ETH, so no ckETH is ever minted. The contract must be redeployed and all integrations (wallets, dApps, the IC minter's `EthereumContractAddress` configuration) must be updated.
- **`CkErc20Deposit`**: every call to `deposit()` reverts; ERC-20 tokens are never transferred and no ckERC20 is minted. Same redeployment requirement.
- **`CkDeposit`**: `depositEth()` sends ETH to `address(0)` (burned); `depositErc20()` sends ERC-20 tokens to `address(0)` (burned). User funds are **permanently lost** with no recovery path. The IC minter canister never observes the `ReceivedEthOrErc20` log events, so no ckETH/ckERC20 is minted.

The chain-fusion bridge is broken at the Ethereum ingress layer. The IC minter canister's log-scraping loop will observe zero matching events and will never mint any wrapped tokens, silently halting the entire ckETH/ckERC20 deposit flow.

---

### Likelihood Explanation

Likelihood is **low** but non-negligible. The deployer must pass `address(0)` by mistake (e.g., a misconfigured deployment script, a missing environment variable, or a copy-paste error). The absence of a guard means the EVM will accept the deployment without any error. The `CkDeposit` case is particularly concerning because the developer already defined `ZERO_ADDRESS` in the same contract, indicating the risk was partially recognized but the constructor was overlooked.

---

### Recommendation

Add a zero-address guard in each constructor before assigning the minter address:

**`EthDepositHelper.sol`:**
```solidity
constructor(address _cketh_minter_main_address) {
    require(_cketh_minter_main_address != address(0), "minter address must not be zero");
    cketh_minter_main_address = payable(_cketh_minter_main_address);
}
```

**`ERC20DepositHelper.sol`:**
```solidity
constructor(address _cketh_minter_main_address) {
    require(_cketh_minter_main_address != address(0), "minter address must not be zero");
    cketh_minter_main_address = _cketh_minter_main_address;
}
```

**`DepositHelperWithSubaccount.sol`:**
```solidity
constructor(address _minterAddress) {
    require(_minterAddress != ZERO_ADDRESS, "minter address must not be zero");
    minterAddress = payable(_minterAddress);
}
```

---

### Proof of Concept

**Scenario for `CkDeposit` (worst case — funds burned):**

1. Deployer runs deployment script with `MINTER_ADDRESS` env var unset (defaults to `""`/`address(0)`).
2. `CkDeposit` is deployed with `minterAddress = address(0)`. No revert occurs.
3. IC minter canister is configured to scrape logs from this contract address.
4. User calls `depositEth{value: 1 ether}(principal, subaccount)`.
5. `minterAddress.transfer(1 ether)` sends 1 ETH to `address(0)` — **permanently burned**.
6. `ReceivedEthOrErc20` event is emitted with `owner = user`, `amount = 1 ether`.
7. IC minter's log scraper picks up the event and attempts to mint ckETH — but the ETH never arrived at the real minter address, so the minter's balance accounting is corrupted.
8. User loses 1 ETH with no ckETH minted. Contract cannot be fixed without redeployment. [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** rs/ethereum/cketh/minter/EthDepositHelper.sol (L10-19)
```text
    address payable private immutable cketh_minter_main_address;

    event ReceivedEth(address indexed from, uint256 value, bytes32 indexed principal);

    /**
     * @dev Set cketh_minter_main_address.
     */
    constructor(address _cketh_minter_main_address) {
        cketh_minter_main_address = payable(_cketh_minter_main_address);
    }
```

**File:** rs/ethereum/cketh/minter/EthDepositHelper.sol (L32-35)
```text
    function deposit(bytes32 _principal) public payable {
        emit ReceivedEth(msg.sender, msg.value, _principal);
        cketh_minter_main_address.transfer(msg.value);
    }
```

**File:** rs/ethereum/cketh/minter/ERC20DepositHelper.sol (L477-485)
```text
    address private immutable cketh_minter_main_address;
    event ReceivedErc20(address indexed erc20_contract_address, address indexed owner, uint256 amount, bytes32 indexed principal);

    /**
     * @dev Set cketh_minter_main_address.
     */
    constructor(address _cketh_minter_main_address) {
        cketh_minter_main_address = _cketh_minter_main_address;
    }
```

**File:** rs/ethereum/cketh/minter/ERC20DepositHelper.sol (L498-500)
```text
    function deposit(address erc20_address, uint256 amount, bytes32 principal) public {
        IERC20 erc20Token = IERC20(erc20_address);
        erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);
```

**File:** rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol (L473-490)
```text
    address constant private ZERO_ADDRESS = address(0);

    address payable private immutable minterAddress;

    event ReceivedEthOrErc20(
        address indexed erc20ContractAddress,
        address indexed owner,
        uint256 amount,
        bytes32 indexed principal,
        bytes32 subaccount
    );

    /**
     * @dev Set cketh_minter_main_address.
     */
    constructor(address _minterAddress) {
        minterAddress = payable(_minterAddress);
    }
```

**File:** rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol (L503-506)
```text
    function depositEth(bytes32 principal, bytes32 subaccount) public payable {
        emit ReceivedEthOrErc20(ZERO_ADDRESS, msg.sender, msg.value, principal, subaccount);
        minterAddress.transfer(msg.value);
    }
```

**File:** rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol (L517-517)
```text
        require(erc20Address != ZERO_ADDRESS, "ERC20: depositErc20 from the zero address");
```
