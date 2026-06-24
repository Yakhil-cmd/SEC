### Title
ckERC20 Minter Mints Tokens Based on Requested Amount, Not Actual Received Amount for Fee-on-Transfer ERC-20 Tokens - (File: `rs/ethereum/cketh/minter/ERC20DepositHelper.sol`, `rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol`, `rs/ethereum/cketh/minter/src/deposit.rs`)

---

### Summary

The ckERC20 deposit helper contracts emit the caller-supplied `amount` in the deposit event, not the actual amount received by the minter's Ethereum address. The IC ckETH minter then mints exactly that event-logged `amount` of ckERC20 tokens on the IC ledger. For fee-on-transfer ERC-20 tokens, the minter's Ethereum address receives `amount - fee`, but the IC ledger mints `amount` ckERC20 tokens, permanently breaking the 1:1 backing invariant.

---

### Finding Description

The ERC-20 deposit flow for ckERC20 works as follows:

1. The user calls `deposit()` / `depositErc20()` on the helper smart contract.
2. The helper contract calls `safeTransferFrom(msg.sender, minterAddress, amount)` and then emits a deposit event containing the caller-supplied `amount`.
3. The IC ckETH minter scrapes these Ethereum logs, parses the `amount` field from the event, and mints exactly that many ckERC20 tokens on the IC ledger.

In `ERC20DepositHelper.sol` (`CkErc20Deposit.deposit`):

```solidity
erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);
emit ReceivedErc20(erc20_address, msg.sender, amount, principal);
``` [1](#0-0) 

In `DepositHelperWithSubaccount.sol` (`CkDeposit.depositErc20`):

```solidity
erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
emit ReceivedEthOrErc20(erc20Address, msg.sender, amount, principal, subaccount);
``` [2](#0-1) 

Both contracts emit the requested `amount` — not the actual balance delta received by `minterAddress`. For a fee-on-transfer ERC-20 token, `safeTransferFrom` deducts a fee from the transferred value, so `minterAddress` receives `amount - fee`, but the event records `amount`.

The IC minter parses this event into a `ReceivedErc20Event` with `value: Erc20Value` set to the event's `amount`: [3](#0-2) 

Then in `deposit.rs`, the `mint()` function mints `event.value()` — the full event-logged amount — to the user:

```rust
amount: event.value(),
``` [4](#0-3) 

There is no step that verifies the minter's actual ERC-20 balance increase before minting ckERC20 tokens.

---

### Impact Explanation

**Ledger conservation bug / chain-fusion mint bug.** The ckERC20 token is designed to be 1:1 backed by the underlying ERC-20 held at the minter's Ethereum address. When a fee-on-transfer ERC-20 is deposited, the minter holds `amount - fee` ERC-20 tokens but mints `amount` ckERC20 tokens. Each such deposit inflates the ckERC20 supply beyond the actual ERC-20 reserves. Over time, the minter becomes insolvent: it cannot honor all ckERC20 withdrawal requests because it holds fewer ERC-20 tokens than the total ckERC20 supply. Users who withdraw last receive nothing or less than expected.

---

### Likelihood Explanation

This requires a fee-on-transfer ERC-20 token to be added as a supported ckERC20 token via NNS governance proposal. USDT (Tether) on Ethereum mainnet has a fee mechanism in its contract that is currently set to 0 but can be enabled by Tether at any time. If USDT is added as a supported ckERC20 token and Tether enables its fee, every deposit would silently over-mint. Additionally, any future deflationary or rebasing ERC-20 token added to the supported list would be immediately exploitable by any unprivileged user simply by calling `depositErc20` on the helper contract.

---

### Recommendation

The helper contracts should emit the actual received amount rather than the caller-supplied `amount`. This can be done by checking the minter's ERC-20 balance before and after the `safeTransferFrom` call:

```solidity
uint256 balanceBefore = erc20Token.balanceOf(minterAddress);
erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
uint256 actualReceived = erc20Token.balanceOf(minterAddress) - balanceBefore;
emit ReceivedEthOrErc20(erc20Address, msg.sender, actualReceived, principal, subaccount);
```

Alternatively, the minter's supported token list should explicitly exclude fee-on-transfer tokens, and the NNS governance process for adding new ckERC20 tokens should include a check for this property.

---

### Proof of Concept

1. Suppose a fee-on-transfer ERC-20 token `FOT` with a 1% transfer fee is added as a supported ckERC20 token.
2. User approves the helper contract to spend 1000 `FOT`.
3. User calls `depositErc20(FOT_address, 1000, principal, subaccount)`.
4. Helper contract calls `safeTransferFrom(user, minter, 1000)`. Due to the 1% fee, minter receives 990 `FOT`. The event emits `amount = 1000`.
5. IC minter scrapes the log, reads `value = 1000`, and mints 1000 ckFOT to the user.
6. User now holds 1000 ckFOT but the minter only holds 990 FOT on Ethereum.
7. Repeating this 100 times: minter holds 99,000 FOT but 100,000 ckFOT are in circulation. The minter is 1% insolvent.
8. The last ~1% of ckFOT holders cannot redeem their tokens. [1](#0-0) [5](#0-4) [4](#0-3) [3](#0-2)

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

**File:** rs/ethereum/cketh/minter/src/eth_logs/mod.rs (L57-75)
```rust
#[derive(Clone, Eq, PartialEq, Ord, PartialOrd, Decode, Encode)]
pub struct ReceivedErc20Event {
    #[n(0)]
    pub transaction_hash: Hash,
    #[n(1)]
    pub block_number: BlockNumber,
    #[cbor(n(2))]
    pub log_index: LogIndex,
    #[n(3)]
    pub from_address: Address,
    #[n(4)]
    pub value: Erc20Value,
    #[cbor(n(5), with = "icrc_cbor::principal")]
    pub principal: Principal,
    #[n(6)]
    pub erc20_contract_address: Address,
    #[n(7)]
    pub subaccount: Option<LedgerSubaccount>,
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
