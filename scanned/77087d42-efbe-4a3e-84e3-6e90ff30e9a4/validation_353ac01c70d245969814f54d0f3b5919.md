### Title
`transfer()` Used Instead of `call()` for ETH Forwarding in ckETH Deposit Helper Contracts — (File: `rs/ethereum/cketh/minter/EthDepositHelper.sol`, `rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol`)

---

### Summary
Both ckETH deposit helper contracts forward user ETH to the minter address using Solidity's `address.transfer()`, which imposes a hard 2300-gas stipend on the recipient. Since EIP-1884 (Istanbul), the gas cost of several opcodes increased, making this stipend insufficient for any recipient that is a smart contract with a non-trivial fallback. If the minter address is ever a contract, every ETH deposit call will revert, permanently blocking the ckETH mint flow for all users.

---

### Finding Description
`CkEthDeposit.deposit()` in `EthDepositHelper.sol` and `CkDeposit.depositEth()` in `DepositHelperWithSubaccount.sol` both forward the deposited ETH to the minter address using `.transfer()`:

**`EthDepositHelper.sol`, line 34:**
```solidity
function deposit(bytes32 _principal) public payable {
    emit ReceivedEth(msg.sender, msg.value, _principal);
    cketh_minter_main_address.transfer(msg.value);   // ← unsafe
}
```

**`DepositHelperWithSubaccount.sol`, line 505:**
```solidity
function depositEth(bytes32 principal, bytes32 subaccount) public payable {
    emit ReceivedEthOrErc20(ZERO_ADDRESS, msg.sender, msg.value, principal, subaccount);
    minterAddress.transfer(msg.value);               // ← unsafe
}
```

`transfer()` caps the gas forwarded to the recipient at 2300. After EIP-1884, opcodes such as `SLOAD` (800 gas), `BALANCE` (700 gas), and `EXTCODEHASH` (700 gas) cost more than they did before Istanbul. Any fallback or `receive()` function in the recipient that touches storage or calls these opcodes will exceed the 2300-gas budget and cause the entire transaction to revert.

Both `cketh_minter_main_address` and `minterAddress` are declared `immutable`, so the address baked in at deployment time is permanent. If a future deployment of either helper contract points to a smart-contract minter address (e.g., a multisig, proxy, or upgraded threshold-ECDSA contract), every call to `deposit()` / `depositEth()` will revert unconditionally.

---

### Impact Explanation
If `transfer()` reverts, the entire transaction is rolled back — including the `ReceivedEth` / `ReceivedEthOrErc20` event. The ckETH minter canister on the IC monitors Ethereum for exactly these events to trigger minting. With no event emitted, the minter never mints ckETH. The result is a **complete denial of service for the ckETH deposit (ETH → ckETH) flow**: no user can convert ETH to ckETH through the affected helper contract. User funds are not lost (the revert returns ETH), but the chain-fusion mint path is broken.

---

### Likelihood Explanation
Currently **low**: the minter address is an EOA derived from threshold ECDSA, so a plain ETH transfer costs well under 2300 gas. However, the risk becomes **medium** upon any redeployment of the helper contract that sets a contract address as the minter (e.g., a multisig treasury, a proxy, or a future on-chain minter). Because the address is `immutable`, there is no upgrade path — a broken deployment is permanently broken. The pattern is also inconsistent with the same file's `depositErc20`, which correctly uses `SafeERC20.safeTransferFrom` (a `call`-based wrapper), signalling that the ETH path was not updated to the same standard.

---

### Recommendation
Replace `.transfer()` with a low-level `.call{value: ...}("")` and check the return value, consistent with the OpenZeppelin `Address.sendValue` pattern already present in `DepositHelperWithSubaccount.sol` (lines 219–228):

```solidity
// EthDepositHelper.sol – deposit()
(bool success, ) = cketh_minter_main_address.call{value: msg.value}("");
require(success, "ETH transfer failed");

// DepositHelperWithSubaccount.sol – depositEth()
(bool success, ) = minterAddress.call{value: msg.value}("");
require(success, "ETH transfer failed");
```

This forwards all available gas, is safe against future gas-cost schedule changes, and is consistent with the `sendValue` helper already defined in the same file.

---

### Proof of Concept

1. Deploy a new instance of `CkEthDeposit` (or `CkDeposit`) with `_cketh_minter_main_address` set to a contract whose `receive()` function executes a single `SLOAD` (costs 800 gas post-Istanbul, exceeding the 2300 stipend when combined with base overhead).
2. Call `deposit{value: 1 ether}(principal)`.
3. The `transfer()` call reverts because the recipient's `receive()` exceeds 2300 gas.
4. The entire transaction reverts; the `ReceivedEth` event is never emitted.
5. The ckETH minter canister sees no deposit event and mints no ckETH — the deposit flow is permanently blocked for this deployment. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** rs/ethereum/cketh/minter/EthDepositHelper.sol (L32-35)
```text
    function deposit(bytes32 _principal) public payable {
        emit ReceivedEth(msg.sender, msg.value, _principal);
        cketh_minter_main_address.transfer(msg.value);
    }
```

**File:** rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol (L219-228)
```text
    function sendValue(address payable recipient, uint256 amount) internal {
        if (address(this).balance < amount) {
            revert AddressInsufficientBalance(address(this));
        }

        (bool success, ) = recipient.call{value: amount}("");
        if (!success) {
            revert FailedInnerCall();
        }
    }
```

**File:** rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol (L503-506)
```text
    function depositEth(bytes32 principal, bytes32 subaccount) public payable {
        emit ReceivedEthOrErc20(ZERO_ADDRESS, msg.sender, msg.value, principal, subaccount);
        minterAddress.transfer(msg.value);
    }
```
