### Title
Missing Zero-Address Validation for `minterAddress` in ckETH/ckERC20 Deposit Helper Constructors - (Files: `rs/ethereum/cketh/minter/EthDepositHelper.sol`, `rs/ethereum/cketh/minter/ERC20DepositHelper.sol`, `rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol`)

---

### Summary

All three production ckETH/ckERC20 chain-fusion deposit helper contracts assign the minter address in their constructors without validating it is non-zero. If any of these contracts is deployed with `address(0)` as the minter address, deposited ETH or ERC20 tokens are permanently burned while the deposit event is still emitted, causing the IC ckETH minter canister to mint unbacked ckETH or ckERC20 tokens.

---

### Finding Description

Three production Solidity contracts in the IC chain-fusion bridge infrastructure share the same missing zero-address guard pattern:

**`EthDepositHelper.sol` — `CkEthDeposit`:** [1](#0-0) 

The constructor assigns `cketh_minter_main_address = payable(_cketh_minter_main_address)` with no `require(_cketh_minter_main_address != address(0))` guard. The `deposit()` function then unconditionally calls `cketh_minter_main_address.transfer(msg.value)`. [2](#0-1) 

**`ERC20DepositHelper.sol` — `CkErc20Deposit`:** [3](#0-2) 

Same pattern: `cketh_minter_main_address = _cketh_minter_main_address` with no zero-address check. The `deposit()` function routes ERC20 tokens directly to this address via `safeTransferFrom`. [4](#0-3) 

**`DepositHelperWithSubaccount.sol` — `CkDeposit`:** [5](#0-4) 

This case is the most notable: the contract explicitly defines `address constant private ZERO_ADDRESS = address(0)` and uses it to guard `erc20Address` in `depositErc20()`, yet the constructor itself does not apply the same guard to `minterAddress`. [6](#0-5) [7](#0-6) 

---

### Impact Explanation

If any of these contracts is deployed with `_minterAddress = address(0)`:

1. **ETH path (`depositEth` / `deposit`):** `minterAddress.transfer(msg.value)` sends ETH to `address(0)`, permanently burning it. The `ReceivedEth` / `ReceivedEthOrErc20` event is still emitted with the correct depositor principal.
2. **ERC20 path (`depositErc20` / `deposit`):** `safeTransferFrom(msg.sender, minterAddress, amount)` transfers tokens to `address(0)`, permanently burning them. The `ReceivedErc20` / `ReceivedEthOrErc20` event is still emitted.
3. The IC ckETH minter canister scrapes these Ethereum logs and mints ckETH or ckERC20 for the depositor based solely on the emitted event — it does not independently verify that the ETH/ERC20 actually arrived at the minter's Ethereum address before minting.

The result is **unbacked ckETH/ckERC20 minting**: the 1:1 peg is broken, user funds are permanently lost on the Ethereum side, and the IC ledger is inflated with tokens that have no on-chain collateral. This is a direct chain-fusion mint/burn integrity bug.

---

### Likelihood Explanation

Likelihood is **low but non-negligible**. The contracts are immutable once deployed (the minter address is `immutable`). A deployment-time error passing `address(0)` — e.g., a misconfigured deployment script, a CI/CD pipeline bug, or a copy-paste error — would permanently lock the contract in a broken state with no upgrade path. The `DepositHelperWithSubaccount.sol` case is particularly risky because the developer demonstrably knew about zero-address checks (they guard `erc20Address`) but omitted the guard in the constructor, indicating the omission is an oversight rather than a deliberate design choice.

---

### Recommendation

Add a zero-address require guard in each constructor:

**`EthDepositHelper.sol`:**
```solidity
constructor(address _cketh_minter_main_address) {
    require(_cketh_minter_main_address != address(0), "minter address cannot be zero");
    cketh_minter_main_address = payable(_cketh_minter_main_address);
}
```

**`ERC20DepositHelper.sol`:**
```solidity
constructor(address _cketh_minter_main_address) {
    require(_cketh_minter_main_address != address(0), "minter address cannot be zero");
    cketh_minter_main_address = _cketh_minter_main_address;
}
```

**`DepositHelperWithSubaccount.sol`:**
```solidity
constructor(address _minterAddress) {
    require(_minterAddress != ZERO_ADDRESS, "minter address cannot be zero");
    minterAddress = payable(_minterAddress);
}
```

---

### Proof of Concept

1. Deploy `CkEthDeposit` (or any of the three contracts) with `_cketh_minter_main_address = address(0)`.
2. Call `deposit(someICPrincipal)` with `msg.value = 1 ether`.
3. Observe: `cketh_minter_main_address.transfer(1 ether)` sends 1 ETH to `address(0)` — permanently burned.
4. Observe: `ReceivedEth(msg.sender, 1 ether, someICPrincipal)` event is emitted on-chain.
5. The IC ckETH minter canister, which scrapes `ReceivedEth` logs from the registered contract address, processes the event and mints 1 ckETH to the depositor's IC principal.
6. Result: 1 ckETH exists on the IC ledger with zero ETH backing — the bridge reserve is undercollateralized by 1 ETH per such deposit.

### Citations

**File:** rs/ethereum/cketh/minter/EthDepositHelper.sol (L17-19)
```text
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

**File:** rs/ethereum/cketh/minter/ERC20DepositHelper.sol (L483-485)
```text
    constructor(address _cketh_minter_main_address) {
        cketh_minter_main_address = _cketh_minter_main_address;
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

**File:** rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol (L473-473)
```text
    address constant private ZERO_ADDRESS = address(0);
```

**File:** rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol (L488-490)
```text
    constructor(address _minterAddress) {
        minterAddress = payable(_minterAddress);
    }
```

**File:** rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol (L517-517)
```text
        require(erc20Address != ZERO_ADDRESS, "ERC20: depositErc20 from the zero address");
```
