### Title
ckERC20 Minter Mints Full Event Amount for Fee-on-Transfer ERC-20 Tokens, Causing Insolvency - (`rs/ethereum/cketh/minter/ERC20DepositHelper.sol`, `rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol`, `rs/ethereum/cketh/minter/src/deposit.rs`)

---

### Summary

The ckERC20 deposit helper smart contracts emit the **requested deposit amount** in their log events, not the **actual amount received** by the minter's Ethereum address. For fee-on-transfer ERC-20 tokens, the actual received amount is `amount - transfer_fee`. The IC minter reads the emitted amount from the log and mints that many ckERC20 tokens to the depositor. This creates a permanent discrepancy: the minter's Ethereum address holds fewer ERC-20 tokens than the total ckERC20 supply, making the system insolvent for that token.

---

### Finding Description

**Step 1 — Helper contract emits the requested amount, not the actual received amount.**

In `ERC20DepositHelper.sol`, the `deposit()` function calls `safeTransferFrom` with `amount` and then emits `ReceivedErc20` with the same `amount` parameter — without performing a balance-before/balance-after check to determine what was actually received:

```solidity
function deposit(address erc20_address, uint256 amount, bytes32 principal) public {
    IERC20 erc20Token = IERC20(erc20_address);
    erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);
    emit ReceivedErc20(erc20_address, msg.sender, amount, principal); // <-- emits requested amount
}
```

The same pattern exists in `DepositHelperWithSubaccount.sol`:

```solidity
erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
emit ReceivedEthOrErc20(erc20Address, msg.sender, amount, principal, subaccount); // <-- emits requested amount
```

For a fee-on-transfer ERC-20 token, `safeTransferFrom` deducts a fee, so `minterAddress` receives `amount - fee`, but the event records `amount`.

**Step 2 — The IC minter parses the emitted amount and mints that many ckERC20 tokens.**

In `rs/ethereum/cketh/minter/src/eth_logs/parser.rs`, `ReceivedErc20LogParser::parse_log` reads the `value` field directly from the event data:

```rust
let [value_bytes] = parse_hex_into_32_byte_words(entry.data, event_source)?;
Ok(ReceivedErc20Event {
    value: Erc20Value::from_be_bytes(value_bytes), // value from event, not actual received
    ...
}.into())
```

In `rs/ethereum/cketh/minter/src/deposit.rs`, the `mint()` function mints `event.value()` tokens — the emitted amount — to the beneficiary:

```rust
let block_index = match client
    .transfer(TransferArg {
        amount: event.value(), // <-- uses emitted amount, not actual received
        ...
    })
    .await
```

**Result:** For every deposit of a fee-on-transfer ERC-20 token, the minter mints `amount` ckERC20 but only holds `amount - fee` of the underlying ERC-20. The deficit accumulates with every deposit.

---

### Impact Explanation

The ckERC20 system becomes insolvent for any supported fee-on-transfer ERC-20 token. The total ckERC20 supply exceeds the ERC-20 balance held at the minter's Ethereum address. When users attempt to withdraw ckERC20 back to ERC-20, the last withdrawers will find insufficient ERC-20 tokens at the minter's address and will be unable to redeem their ckERC20 tokens. The deficit equals the cumulative transfer fees across all deposits. For a token with a 1% transfer fee and 100M tokens deposited, the deficit is 1M tokens — a direct, permanent loss borne by the last redeemers.

---

### Likelihood Explanation

This requires a fee-on-transfer ERC-20 token to be added as a supported ckERC20 token. USDT (Tether) has a transfer fee mechanism in its contract that is currently set to zero but can be enabled by the USDT issuer at any time. If USDT is or becomes a supported ckERC20 token and its fee is enabled, this vulnerability is immediately triggered by any ordinary user deposit. Additionally, new ckERC20 tokens with transfer fees could be added via NNS governance proposals. No privileged access is required to trigger the bug — any user calling `depositErc20` on the helper contract with a fee-on-transfer token is sufficient.

---

### Recommendation

1. **In the Solidity helper contracts** (`ERC20DepositHelper.sol`, `DepositHelperWithSubaccount.sol`): Use a balance-before/balance-after pattern to determine the actual received amount and emit that in the event instead of the requested `amount`:

```solidity
uint256 balanceBefore = IERC20(erc20Address).balanceOf(minterAddress);
erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
uint256 actualReceived = IERC20(erc20Address).balanceOf(minterAddress) - balanceBefore;
emit ReceivedEthOrErc20(erc20Address, msg.sender, actualReceived, principal, subaccount);
```

2. **In the minter governance**: Add a check when adding new ckERC20 tokens to reject tokens that have transfer fees enabled, or document that fee-on-transfer tokens are not supported.

---

### Proof of Concept

1. A fee-on-transfer ERC-20 token (e.g., a hypothetical `FeeToken` with a 2% transfer fee) is added as a supported ckERC20 token via NNS proposal.
2. Alice calls `depositErc20(feeTokenAddress, 1000, alicePrincipal, subaccount)` on the helper contract.
3. The helper calls `safeTransferFrom(Alice, minterAddress, 1000)`. Due to the 2% fee, `minterAddress` receives only 980 tokens.
4. The helper emits `ReceivedEthOrErc20(feeTokenAddress, Alice, 1000, alicePrincipal, subaccount)` — recording 1000, not 980.
5. The IC minter scrapes the log, reads `value = 1000`, and mints 1000 ckFeeToken to Alice.
6. After N such deposits totaling 100,000 requested tokens, the minter holds only 98,000 actual tokens but has minted 100,000 ckFeeToken.
7. The first 98 withdrawers successfully redeem their ckFeeToken for FeeToken. The last depositors find the minter's Ethereum address has insufficient balance and cannot withdraw.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** rs/ethereum/cketh/minter/ERC20DepositHelper.sol (L498-503)
```text
    function deposit(address erc20_address, uint256 amount, bytes32 principal) public {
        IERC20 erc20Token = IERC20(erc20_address);
        erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);

        emit ReceivedErc20(erc20_address, msg.sender, amount, principal);
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

**File:** rs/ethereum/cketh/minter/src/eth_logs/parser.rs (L86-103)
```rust
        let [value_bytes] = parse_hex_into_32_byte_words(entry.data, event_source)?;
        let EventSource {
            transaction_hash,
            log_index,
        } = event_source;

        Ok(ReceivedErc20Event {
            transaction_hash,
            block_number,
            log_index,
            from_address,
            value: Erc20Value::from_be_bytes(value_bytes),
            principal,
            erc20_contract_address,
            subaccount: None,
        }
        .into())
    }
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
