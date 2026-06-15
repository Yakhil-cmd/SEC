### Title
`WSEI.withdraw()` Uses `.transfer()` Opcode, Permanently Blocking Contract-Caller Withdrawals — (File: `contracts/src/WSEI.sol`)

---

### Summary

The `withdraw` function in the Sei-native `WSEI` (Wrapped SEI) contract uses `payable(msg.sender).transfer(wad)` to return native SEI to the caller. This hard-caps the forwarded gas at 2300, causing the call to revert for any contract caller whose `receive`/`fallback` function consumes more than 2300 gas. Affected contract callers can never unwrap their WSEI back to native SEI.

---

### Finding Description

In `contracts/src/WSEI.sol`, the `withdraw` function is:

```solidity
function withdraw(uint wad) public {
    require(balanceOf[msg.sender] >= wad);
    balanceOf[msg.sender] -= wad;
    payable(msg.sender).transfer(wad);   // ← 2300 gas stipend only
    emit Withdrawal(msg.sender, wad);
}
``` [1](#0-0) 

Solidity's `.transfer()` forwards exactly 2300 gas. Any contract that holds WSEI and whose `receive` or `fallback` function performs even minimal logic (e.g., emitting an event, updating a state variable, calling another contract) will exceed 2300 gas. When that happens, `.transfer()` reverts the entire transaction, and the caller's WSEI balance is restored — but the caller is permanently unable to unwrap.

This is not a theoretical concern: multisig wallets (Gnosis Safe), proxy contracts, and any DeFi protocol that holds WSEI on behalf of users all have `receive`/`fallback` functions that exceed 2300 gas. EIP-1884 (Istanbul hard fork) already demonstrated that previously safe 2300-gas assumptions can break when opcode costs change.

The same pattern appears in `evmrpc/solidity/ERC20.sol` (line 25), but that file is co-located with RPC test helpers and is excluded from scope. [2](#0-1) 

---

### Impact Explanation

Any contract address that has called `deposit()` and holds a WSEI balance cannot call `withdraw()` successfully if its `receive`/`fallback` requires >2300 gas. The funds are not permanently destroyed (the WSEI token balance remains), but the holder is permanently unable to convert WSEI back to native SEI through the canonical path. For DeFi protocols or multisigs holding WSEI, this constitutes a fund freeze. Severity maps to **Medium** (fund freeze; total amount depends on WSEI TVL held by contract addresses).

---

### Likelihood Explanation

Likelihood is **Medium**. The condition requires `msg.sender` to be a contract with a non-trivial `receive`/`fallback`. This is common in practice: Gnosis Safe multisigs, proxy-based wallets, yield aggregators, and any protocol that auto-compounds or routes SEI through WSEI all qualify. A user or protocol that deposits into WSEI from such a contract will discover the issue only at withdrawal time, with no recourse.

---

### Recommendation

Replace `.transfer()` with a low-level `.call` and add a reentrancy guard:

```solidity
import "@openzeppelin/contracts/security/ReentrancyGuard.sol";

function withdraw(uint wad) public nonReentrant {
    require(balanceOf[msg.sender] >= wad);
    balanceOf[msg.sender] -= wad;
    (bool success, ) = payable(msg.sender).call{value: wad}("");
    require(success, "ETH transfer failed");
    emit Withdrawal(msg.sender, wad);
}
```

The CEI (Checks-Effects-Interactions) order is already correct (balance decremented before the external call), so adding `nonReentrant` is a defence-in-depth measure.

---

### Proof of Concept

1. Deploy a contract `Vault` on Sei EVM that:
   - Has a `receive()` function that writes to storage (costs >2300 gas).
   - Calls `WSEI.deposit{value: 1 ether}()` during construction.
2. Later call `WSEI.withdraw(1 ether)` from `Vault`.
3. The call reverts with out-of-gas inside `.transfer()`.
4. `Vault`'s WSEI balance remains non-zero; native SEI is permanently locked in WSEI for this caller. [3](#0-2)

### Citations

**File:** contracts/src/WSEI.sol (L17-32)
```text
    fallback() external payable {
        deposit();
    }
    receive() external payable {
        deposit();
    }
    function deposit() public payable {
        balanceOf[msg.sender] += msg.value;
        emit Deposit(msg.sender, msg.value);
    }
    function withdraw(uint wad) public {
        require(balanceOf[msg.sender] >= wad);
        balanceOf[msg.sender] -= wad;
        payable(msg.sender).transfer(wad);
        emit Withdrawal(msg.sender, wad);
    }
```
