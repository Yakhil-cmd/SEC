### Title
Smart Contract Wallets Cannot Withdraw WSEI Due to `transfer()` Gas Stipend Limit - (File: `contracts/src/WSEI.sol`)

---

### Summary

The predeployed `WSEI` contract on Sei uses `payable(msg.sender).transfer(wad)` in its `withdraw` function. This forwards only 2300 gas to the recipient, which is insufficient for smart contract wallets (e.g., Safe/Gnosis multisig, ERC-4337 accounts) that execute logic in their `receive()` function. Any such wallet calling `withdraw()` will receive an out-of-gas revert, making it impossible to unwrap WSEI directly.

---

### Finding Description

The `withdraw` function in `WSEI.sol` uses Solidity's `transfer()` primitive:

```solidity
function withdraw(uint wad) public {
    require(balanceOf[msg.sender] >= wad);
    balanceOf[msg.sender] -= wad;
    payable(msg.sender).transfer(wad);   // ← only 2300 gas forwarded
    emit Withdrawal(msg.sender, wad);
}
``` [1](#0-0) 

`transfer()` hard-caps the gas forwarded to the recipient at 2300. This is enough for a simple EOA receive, but not for any contract that performs storage reads/writes, emits events, or calls other contracts in its `receive()` or `fallback()`. The compiled bytecode of this contract is embedded in the chain binary and deployed via the `CmdDeployWSEI` CLI command as the canonical WSEI contract. [2](#0-1) [3](#0-2) 

---

### Impact Explanation

Any smart contract wallet (multisig, ERC-4337 account abstraction, proxy wallet) that holds WSEI and attempts to call `withdraw()` will have the transaction revert with out-of-gas. The user's WSEI balance remains intact (the balance deduction is reverted), but they cannot convert WSEI back to native SEI directly. They must first `transfer()` their WSEI tokens to an EOA and withdraw from there — a non-obvious workaround that breaks composability.

Funds are not permanently frozen (the ERC-20 `transfer` path is unaffected), so this is a **Medium** severity issue under Sei's scope: unintended failure of contract execution due to a network-level contract design bug, with fund accessibility impact below $5k per individual transaction.

---

### Likelihood Explanation

Smart contract wallets are increasingly common (Safe, Coinbase Smart Wallet, ERC-4337 accounts). Any such wallet that wraps SEI into WSEI and later tries to unwrap it will hit this failure. The path is fully unprivileged — no special role or key is needed. The attacker surface is passive: the bug triggers on normal user behavior.

---

### Recommendation

Replace `payable(msg.sender).transfer(wad)` with a low-level `call` and add a reentrancy guard (the balance is already decremented before the call, satisfying checks-effects-interactions):

```solidity
bool success;
(success, ) = payable(msg.sender).call{value: wad}("");
require(success, "ETH transfer failed");
```

This mirrors the fix applied in the referenced Scroll PRs (#558, #632) that replaced `WETH9.sol` with `WrappedEther.sol`.

---

### Proof of Concept

1. Deploy a smart contract wallet `SmartWallet` with a `receive()` that writes to storage (costs >2300 gas).
2. From `SmartWallet`, call `WSEI.deposit{value: 1 ether}()` — succeeds.
3. From `SmartWallet`, call `WSEI.withdraw(1 ether)` — reverts with out-of-gas because `transfer()` only forwards 2300 gas to `SmartWallet.receive()`.
4. `SmartWallet`'s WSEI balance is unchanged (revert rolls back the deduction), but the wallet cannot unwrap without routing through an EOA. [1](#0-0)

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

**File:** x/evm/artifacts/wsei/artifacts.go (L11-15)
```go
const CurrentVersion uint16 = 1

//go:embed WSEI.abi
//go:embed WSEI.bin
var f embed.FS
```

**File:** x/evm/client/cli/tx.go (L515-522)
```go
func CmdDeployWSEI() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "deploy-wsei --from=<sender> --gas-fee-cap=<cap> --gas-limt=<limit> --evm-rpc=<url>",
		Short: "Deploy ERC20 contract for a native Sei token",
		Long:  "",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) (err error) {
			contractData := wsei.GetBin()
```
