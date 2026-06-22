### Title
Fee-on-Transfer ERC-20 Tokens Cause ckERC20 Over-Minting Due to Emitting Requested Amount Instead of Actual Received Amount - (File: rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol, rs/ethereum/cketh/minter/ERC20DepositHelper.sol)

---

### Summary

The ckERC20 deposit helper smart contracts emit the caller-supplied `amount` parameter in the `ReceivedEthOrErc20` / `ReceivedErc20` event rather than the actual amount received by the minter address after `safeTransferFrom`. The IC ckETH minter canister reads this event value and mints exactly that many ckERC20 tokens. For fee-on-transfer (deflationary) ERC-20 tokens, the minter receives fewer tokens than the event claims, causing it to mint more ckERC20 than the ERC-20 it holds, permanently breaking the 1:1 backing invariant.

---

### Finding Description

**Root cause in `DepositHelperWithSubaccount.sol` (`CkDeposit.depositErc20`):**

```solidity
function depositErc20(address erc20Address, uint256 amount, ...) public {
    IERC20 erc20Token = IERC20(erc20Address);
    erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);  // actual received = amount - fee

    emit ReceivedEthOrErc20(
        erc20Address,
        msg.sender,
        amount,          // ← emits the requested amount, NOT the actual received amount
        principal,
        subaccount
    );
}
```

The same pattern exists in `ERC20DepositHelper.sol` (`CkErc20Deposit.deposit`):

```solidity
erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);
emit ReceivedErc20(erc20_address, msg.sender, amount, principal);  // ← same issue
```

**IC minter canister mints the event value verbatim (`deposit.rs`):**

```rust
let block_index = match client
    .transfer(TransferArg {
        amount: event.value(),  // ← trusts the event amount directly
        ...
    })
    .await
```

`event.value()` returns the `value` field parsed directly from the Ethereum log data, which is the `amount` parameter the caller passed to `depositErc20`, not the actual balance change at `minterAddress`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

---

### Impact Explanation

For any fee-on-transfer ERC-20 token added as a supported ckERC20 token:

- User calls `depositErc20(feeToken, 1000, ...)`.
- Token contract deducts a 1% fee; minter receives 990 tokens.
- Helper contract emits `ReceivedEthOrErc20(..., 1000, ...)`.
- IC minter mints 1000 ckERC20 to the user.
- Minter holds 990 ERC-20 but has issued 1000 ckERC20 — a 10-token deficit per deposit.

Repeated deposits drain the minter's ERC-20 reserves. When other users attempt to withdraw ckERC20 back to ERC-20, the minter will eventually be unable to fulfill withdrawals, causing permanent loss of funds for honest depositors. The 1:1 backing invariant — the core security property of the chain-fusion twin-token design — is violated. [5](#0-4) 

---

### Likelihood Explanation

The trigger condition is that a fee-on-transfer ERC-20 token is added as a supported ckERC20 token. This can occur via:

1. **NNS governance proposal** adding a deflationary token (e.g., tokens with auto-staking fees, reflection tokens, or tokens with configurable transfer taxes).
2. **An existing supported token upgrading its implementation** (via a proxy upgrade) to add a transfer fee — a realistic scenario for upgradeable ERC-20 proxies such as USDT or USDC, which have historically modified their contracts.

The second path requires no IC governance action and is triggered purely by an external contract upgrade. The IC minter has no mechanism to detect or reject fee-on-transfer tokens at deposit time. Likelihood is medium given the growing ecosystem of ckERC20 tokens and the prevalence of upgradeable ERC-20 contracts.

---

### Recommendation

The helper contracts should measure the actual balance change at `minterAddress` rather than trusting the caller-supplied `amount`:

```solidity
function depositErc20(address erc20Address, uint256 amount, bytes32 principal, bytes32 subaccount) public {
    IERC20 erc20Token = IERC20(erc20Address);
    uint256 balanceBefore = erc20Token.balanceOf(minterAddress);
    erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
    uint256 actualReceived = erc20Token.balanceOf(minterAddress) - balanceBefore;

    emit ReceivedEthOrErc20(
        erc20Address,
        msg.sender,
        actualReceived,   // ← emit actual received amount
        principal,
        subaccount
    );
}
```

This mirrors the fix recommended in the external report: capture the return value (or in this case, the balance delta) and use it as the authoritative minted amount.

---

### Proof of Concept

1. A fee-on-transfer ERC-20 token `FeeToken` (1% fee on every transfer) is added as a supported ckERC20 token via NNS proposal.
2. Attacker calls `depositErc20(FeeToken, 10_000, principal, subaccount)` on `CkDeposit`.
3. `safeTransferFrom` transfers 10_000 tokens; `FeeToken` deducts 100 as fee; minter receives 9_900.
4. Helper emits `ReceivedEthOrErc20(FeeToken, attacker, 10_000, principal, subaccount)`.
5. IC minter scrapes the log, reads `value = 10_000`, and mints 10_000 ckFeeToken to attacker.
6. Attacker holds 10_000 ckFeeToken; minter holds only 9_900 FeeToken.
7. Attacker immediately withdraws 10_000 ckFeeToken → minter attempts to send 10_000 FeeToken but only has 9_900, causing the last 100 tokens worth of withdrawals to fail for other users.
8. Repeating this attack drains the minter's ERC-20 reserves entirely. [6](#0-5) [7](#0-6)

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

**File:** rs/ethereum/cketh/minter/ERC20DepositHelper.sol (L498-503)
```text
    function deposit(address erc20_address, uint256 amount, bytes32 principal) public {
        IERC20 erc20Token = IERC20(erc20_address);
        erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);

        emit ReceivedErc20(erc20_address, msg.sender, amount, principal);
    }
```

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L73-102)
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
            .await
        {
            Ok(Ok(block_index)) => block_index.0.to_u64().expect("nat does not fit into u64"),
            Ok(Err(err)) => {
                log!(INFO, "Failed to mint {token_symbol}: {event:?} {err}");
                error_count += 1;
                // minting failed, defuse guard
                ScopeGuard::into_inner(prevent_double_minting_guard);
                continue;
            }
            Err(err) => {
                log!(
                    INFO,
                    "Failed to send a message to the ledger ({ledger_canister_id}): {err:?}"
                );
                error_count += 1;
                // minting failed, defuse guard
                ScopeGuard::into_inner(prevent_double_minting_guard);
                continue;
            }
        };
```

**File:** rs/ethereum/cketh/minter/src/eth_logs/mod.rs (L205-210)
```rust
    pub fn value(&self) -> candid::Nat {
        match self {
            ReceivedEvent::Eth(evt) => evt.value.into(),
            ReceivedEvent::Erc20(evt) => evt.value.into(),
        }
    }
```
