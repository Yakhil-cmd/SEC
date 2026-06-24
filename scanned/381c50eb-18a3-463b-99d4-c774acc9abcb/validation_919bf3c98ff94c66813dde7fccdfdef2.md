### Title
Fee-on-Transfer ERC-20 Overminting: `depositErc20` Emits Pre-Fee `amount` While Minter Mints That Full Amount - (File: rs/ethereum/cketh/minter/ERC20DepositHelper.sol and rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol)

---

### Summary

The ckERC20 deposit helper smart contracts (`CkErc20Deposit.deposit` and `CkDeposit.depositErc20`) emit a `ReceivedErc20` / `ReceivedEthOrErc20` event carrying the caller-supplied `amount` parameter. For fee-on-transfer (FoT) ERC-20 tokens, the actual tokens received by the minter address are `amount - fee`, but the event still records the full `amount`. The IC ckETH minter canister reads this event value and mints exactly `event.value()` ckERC20 tokens to the user. The result is that the user receives more ckERC20 than the minter actually holds in ERC-20 collateral, breaking the 1:1 peg and draining the minter's ERC-20 reserve.

---

### Finding Description

**Ethereum side — helper contracts:**

`CkErc20Deposit.deposit` in `ERC20DepositHelper.sol`:

```solidity
function deposit(address erc20_address, uint256 amount, bytes32 principal) public {
    IERC20 erc20Token = IERC20(erc20_address);
    erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);
    emit ReceivedErc20(erc20_address, msg.sender, amount, principal);  // ← always `amount`, not actual received
}
```

`CkDeposit.depositErc20` in `DepositHelperWithSubaccount.sol`:

```solidity
erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
emit ReceivedEthOrErc20(erc20Address, msg.sender, amount, principal, subaccount);  // ← same issue
```

For a standard ERC-20, `safeTransferFrom` delivers exactly `amount` to `minterAddress`. For a FoT token, the minter receives `amount - fee`, but the event still logs `amount`.

**IC side — minter canister:**

The minter scrapes Ethereum logs, parses the `ReceivedErc20` / `ReceivedEthOrErc20` event, and stores `event.value` (the logged `amount`) as `ReceivedErc20Event.value`:

```rust
// rs/ethereum/cketh/minter/src/eth_logs/parser.rs
let value = Erc20Value::from_be_bytes(value_bytes);  // value_bytes from event data = `amount`
Ok(ReceivedErc20Event { ..., value, ... }.into())
```

The `mint()` function in `rs/ethereum/cketh/minter/src/deposit.rs` then mints exactly `event.value()` ckERC20 tokens:

```rust
amount: event.value(),   // ← the over-stated amount from the log
```

There is no balance-before/after check anywhere in the IC minter to reconcile the logged amount against what was actually received.

---

### Impact Explanation

**Ledger conservation bug / chain-fusion mint/burn/replay bug.**

For any FoT ERC-20 token that is added as a supported ckERC20 token:

1. A user deposits `N` tokens; the minter receives `N - fee` tokens.
2. The event logs `N`; the minter mints `N` ckERC20 to the user.
3. The minter's ERC-20 reserve is short by `fee` per deposit.
4. After enough deposits, the minter cannot fulfill all withdrawal requests — the last withdrawers receive fewer ERC-20 tokens than their ckERC20 balance entitles them to, or withdrawals fail entirely.
5. An attacker who controls or deploys a FoT ERC-20 and gets it listed as a supported ckERC20 token can systematically drain the minter's reserve by repeatedly depositing, receiving over-minted ckERC20, and withdrawing.

The impact is a permanent, irreversible break of the 1:1 peg for the affected ckERC20 token, with direct financial loss to honest users who withdraw last.

---

### Likelihood Explanation

The ckETH minter already supports multiple ERC-20 tokens (ckUSDC, ckUSDT, etc.) and new tokens are added via NNS governance proposals. The vulnerability is latent for any currently-listed token that introduces a transfer fee in the future (some tokens have upgradeable fee mechanisms), and is immediately exploitable for any FoT token that is proposed and accepted. The attack requires only a standard Ethereum transaction calling `depositErc20` — no privileged access, no key compromise, no threshold attack. The attacker-controlled entry path is a normal unprivileged Ethereum user calling the public helper contract.

---

### Recommendation

1. **In the helper contracts**: record the actual received amount by checking the minter's balance before and after the `safeTransferFrom`, and emit that delta in the event:

```solidity
uint256 balanceBefore = IERC20(erc20Address).balanceOf(minterAddress);
erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
uint256 balanceAfter = IERC20(erc20Address).balanceOf(minterAddress);
emit ReceivedEthOrErc20(erc20Address, msg.sender, balanceAfter - balanceBefore, principal, subaccount);
```

2. **In the IC minter**: add a documentation/governance-level restriction that FoT or rebasing ERC-20 tokens must not be added as supported ckERC20 tokens. The NNS proposal process for `AddCkErc20Token` should include an explicit check or attestation that the token does not charge transfer fees.

3. Alternatively, the IC minter could verify the actual ERC-20 balance of the minter address via an `eth_call` to `balanceOf` before and after processing a deposit batch, and use the delta rather than the logged value — though this is architecturally more complex given the async log-scraping model.

---

### Proof of Concept

1. Deploy a FoT ERC-20 token on Ethereum with a 1% transfer fee.
2. Get it listed as a supported ckERC20 token via NNS proposal.
3. Call `depositErc20(fot_token, 1_000_000, principal, subaccount)` on the helper contract.
4. The minter receives `990_000` tokens (1% fee deducted by the token contract).
5. The `ReceivedErc20` event logs `amount = 1_000_000`.
6. The IC minter scrapes the log, reads `value = 1_000_000`, and mints `1_000_000` ckFOT to the user.
7. The user now holds `1_000_000` ckFOT backed by only `990_000` real tokens.
8. Repeat until the minter's reserve is exhausted; the last withdrawer cannot redeem their full ckFOT balance.

**Root cause files:**
- [1](#0-0) 
- [2](#0-1) 
- [3](#0-2) 
- [4](#0-3)

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
