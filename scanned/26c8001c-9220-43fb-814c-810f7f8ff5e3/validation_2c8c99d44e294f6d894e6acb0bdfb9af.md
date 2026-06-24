Now I have all the information needed to write the finding. The analog is clear: both deposit helper contracts emit the pre-fee `amount` in the event, while the IC minter mints exactly that `amount` of ckERC20 — creating undercollateralization if the ERC20 token charges a transfer fee.

---

### Title
Fee-on-Transfer ERC20 Tokens Cause ckERC20 Undercollateralization via Inflated Deposit Event Amount — (`rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol`, `rs/ethereum/cketh/minter/ERC20DepositHelper.sol`)

### Summary

Both ckERC20 deposit helper contracts (`CkDeposit.depositErc20` and `CkErc20Deposit.deposit`) emit a deposit event carrying the caller-supplied `amount` parameter, not the actual tokens received by the minter address. The IC ckETH minter canister scrapes these events and mints exactly `event.value()` ckERC20 tokens to the user. If the underlying ERC20 token charges a fee on transfer, the minter receives fewer tokens than the event reports, but mints the full reported amount — permanently undercollateralizing the ckERC20 token.

### Finding Description

`CkDeposit.depositErc20` in `DepositHelperWithSubaccount.sol`:

```solidity
erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
emit ReceivedEthOrErc20(erc20Address, msg.sender, amount, principal, subaccount);
``` [1](#0-0) 

`CkErc20Deposit.deposit` in `ERC20DepositHelper.sol`:

```solidity
erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);
emit ReceivedErc20(erc20_address, msg.sender, amount, principal);
``` [2](#0-1) 

In both cases, the emitted `amount` is the caller-supplied argument, not the balance delta actually received by the minter address. For a fee-on-transfer ERC20, the minter receives `amount - fee` but the event records `amount`.

The IC minter canister parses the `value` field directly from the event log: [3](#0-2) 

It then mints exactly `event.value()` ckERC20 tokens to the beneficiary: [4](#0-3) 

There is no balance-delta check anywhere in the minting pipeline. The minter unconditionally trusts the `amount` field in the event.

### Impact Explanation

Every deposit of a fee-on-transfer ERC20 token mints more ckERC20 than the minter holds in ERC20 backing. The ckERC20 token supply grows faster than the minter's ERC20 reserve. When users attempt to withdraw ckERC20 back to ERC20, the minter will eventually be unable to fulfill withdrawals — a direct, permanent loss of funds for ckERC20 holders. The deficit compounds with every deposit.

### Likelihood Explanation

The minter enforces a whitelist of supported ERC20 tokens. Currently supported tokens (e.g., USDC) do not charge transfer fees. However, USDT — a prominent stablecoin with an upgradeable contract that includes a fee-on-transfer mechanism in its code — is a realistic future addition. If USDT or any other upgradeable token with latent fee logic is added to the supported list, or if an already-supported token enables fees via an upgrade, this vulnerability becomes immediately exploitable by any depositor without any privileged access.

### Recommendation

Replace the emitted `amount` with the actual balance delta received by the minter address. In `depositErc20` / `deposit`, record the minter's ERC20 balance before and after the `safeTransferFrom` call and emit the difference:

```solidity
uint256 balanceBefore = erc20Token.balanceOf(minterAddress);
erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
uint256 received = erc20Token.balanceOf(minterAddress) - balanceBefore;
emit ReceivedEthOrErc20(erc20Address, msg.sender, received, principal, subaccount);
```

This ensures the IC minter only mints ckERC20 tokens equal to the ERC20 tokens actually received, regardless of any fee-on-transfer behavior.

### Proof of Concept

1. A fee-on-transfer ERC20 token (e.g., USDT after enabling its fee) is added to the minter's supported token list.
2. An unprivileged user calls `depositErc20(tokenAddress, 1000, principal, subaccount)` on the `CkDeposit` helper contract.
3. The token's `transferFrom` deducts a 1% fee: the minter receives 990 tokens, but the `ReceivedEthOrErc20` event records `amount = 1000`.
4. The IC minter scrapes the event via `ReceivedEthOrErc20LogParser::parse_log`, reads `value = 1000`, and calls `icrc1_transfer` to mint 1000 ckERC20 to the user.
5. The minter's ERC20 reserve is now 990, but 1000 ckERC20 are in circulation — a 10-token deficit per deposit.
6. Repeated deposits widen the deficit until the minter cannot honor withdrawals. [5](#0-4) [3](#0-2) [4](#0-3)

### Citations

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

**File:** rs/ethereum/cketh/minter/ERC20DepositHelper.sol (L499-503)
```text
        IERC20 erc20Token = IERC20(erc20_address);
        erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);

        emit ReceivedErc20(erc20_address, msg.sender, amount, principal);
    }
```

**File:** rs/ethereum/cketh/minter/src/eth_logs/parser.rs (L149-160)
```rust
        let value = Erc20Value::from_be_bytes(value_bytes);
        Ok(ReceivedErc20Event {
            transaction_hash,
            block_number,
            log_index,
            from_address,
            value,
            principal,
            erc20_contract_address,
            subaccount,
        }
        .into())
```

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L73-81)
```rust
        let block_index = match client
            .transfer(TransferArg {
                from_subaccount: None,
                to: event.beneficiary(),
                fee: None,
                created_at_time: None,
                memo: Some((&event).into()),
                amount: event.value(),
            })
```
