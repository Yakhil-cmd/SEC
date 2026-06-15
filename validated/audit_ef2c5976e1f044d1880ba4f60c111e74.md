### Title
`payable.transfer()` in `WSEI.withdraw()` Permanently Locks Funds for Smart Contract Callers - (File: `contracts/src/WSEI.sol`)

---

### Summary

The canonical `WSEI` (Wrapped SEI) contract, whose compiled artifacts are embedded directly into the sei-chain binary, uses `payable(msg.sender).transfer(wad)` in its `withdraw` function. This forwards a hard-capped 2300 gas stipend, which is insufficient for any smart contract recipient that has a non-trivial `receive`/`fallback` function. Any WSEI held by such a contract becomes permanently unwithdrawable.

---

### Finding Description

`WSEI.sol` is not an example or test contract — its ABI and bytecode are embedded in the chain binary via `x/evm/artifacts/wsei/artifacts.go` and deployed as a canonical chain artifact. [1](#0-0) 

```solidity
function withdraw(uint wad) public {
    require(balanceOf[msg.sender] >= wad);
    balanceOf[msg.sender] -= wad;
    payable(msg.sender).transfer(wad);   // ← 2300 gas cap
    emit Withdrawal(msg.sender, wad);
}
``` [2](#0-1) 

The `transfer()` call forwards exactly 2300 gas. This is insufficient when `msg.sender` is:
- A multisig wallet (e.g., Gnosis Safe) whose `receive` logic exceeds 2300 gas
- A DeFi vault or proxy contract with a non-trivial fallback
- Any contract called through a proxy, where the extra `DELEGATECALL` overhead pushes gas usage above 2300

The state update (`balanceOf[msg.sender] -= wad`) executes before the transfer, so the balance is already deducted when the transfer reverts — but because the entire transaction reverts on `transfer()` failure, the balance deduction is also rolled back. The net effect is that the `withdraw()` call simply always reverts for these callers, permanently trapping their WSEI.

---

### Impact Explanation

Any smart contract that deposits SEI into WSEI (via `deposit()` or direct ETH send) and later attempts to call `withdraw()` will have the call revert unconditionally. The deposited SEI is locked inside the WSEI contract with no alternative withdrawal path. This constitutes a fund freeze for the affected caller. The severity scales with WSEI adoption by smart contract wallets and DeFi integrations on Sei.

Scope match: **Medium** — fund freeze, unprivileged path, sei-chain canonical contract is the necessary vulnerable component.

---

### Likelihood Explanation

- Multisig wallets (Gnosis Safe and equivalents) are the standard treasury management tool for DeFi protocols; their `receive` functions routinely exceed 2300 gas.
- Any protocol that wraps SEI programmatically (e.g., a yield vault, an AMM router) will hit this on unwrap.
- No special privileges are required; any contract that calls `deposit()` followed by `withdraw()` triggers the bug.

---

### Recommendation

Replace `transfer()` with a low-level `call` and check the return value:

```solidity
function withdraw(uint wad) public {
    require(balanceOf[msg.sender] >= wad);
    balanceOf[msg.sender] -= wad;
    (bool success, ) = payable(msg.sender).call{value: wad}("");
    require(success, "WSEI: transfer failed");
    emit Withdrawal(msg.sender, wad);
}
```

Add a reentrancy guard (e.g., OpenZeppelin `ReentrancyGuard`) since `call` forwards all remaining gas and the balance update already precedes the external call (checks-effects-interactions is satisfied here, but a guard is best practice).

---

### Proof of Concept

1. Deploy a contract `Vault` with a `receive()` function that does a non-trivial operation (e.g., emits an event — costs ~375 gas, already within 2300, but a storage write costs ~20000 gas).
2. From `Vault`, call `WSEI.deposit{value: 1 ether}()`.
3. From `Vault`, call `WSEI.withdraw(1 ether)`.
4. The call reverts because `Vault.receive()` requires more than 2300 gas; the 1 WSEI balance remains credited but is unwithdrawable via any path. [1](#0-0) [3](#0-2)

### Citations

**File:** contracts/src/WSEI.sol (L27-32)
```text
    function withdraw(uint wad) public {
        require(balanceOf[msg.sender] >= wad);
        balanceOf[msg.sender] -= wad;
        payable(msg.sender).transfer(wad);
        emit Withdrawal(msg.sender, wad);
    }
```

**File:** x/evm/artifacts/wsei/artifacts.go (L1-15)
```go
package wsei

import (
	"embed"
	"encoding/hex"
	"strings"

	"github.com/ethereum/go-ethereum/accounts/abi"
)

const CurrentVersion uint16 = 1

//go:embed WSEI.abi
//go:embed WSEI.bin
var f embed.FS
```
