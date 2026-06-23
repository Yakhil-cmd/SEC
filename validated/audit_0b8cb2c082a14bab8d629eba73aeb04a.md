### Title
ckERC20 Minter Over-Mints Tokens for Fee-on-Transfer ERC20 Deposits, Breaking the 1:1 Peg - (File: `rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol`, `rs/ethereum/cketh/minter/ERC20DepositHelper.sol`, `rs/ethereum/cketh/minter/src/deposit.rs`)

---

### Summary

The ckERC20 deposit helper smart contracts emit a `ReceivedEthOrErc20` / `ReceivedErc20` event carrying the caller-supplied `amount` parameter, not the actual tokens received by the minter's Ethereum address. The ckETH minter canister scrapes these logs and unconditionally mints `event.value()` ckERC20 tokens on the IC. For ERC20 tokens that charge a fee on every `transferFrom` (fee-on-transfer tokens), the minter's address receives fewer tokens than `amount`, yet the minter mints the full `amount` of ckERC20. This breaks the 1:1 backing guarantee and inflates the ckERC20 supply beyond the actual ERC20 collateral held.

---

### Finding Description

**Step 1 – Helper contract emits the requested amount, not the received amount.**

In `DepositHelperWithSubaccount.sol`, `depositErc20` calls `safeTransferFrom(msg.sender, minterAddress, amount)` and then emits `ReceivedEthOrErc20(erc20Address, msg.sender, amount, principal, subaccount)`: [1](#0-0) 

The `amount` field in the emitted event is the *input parameter*, not the balance delta actually credited to `minterAddress`. For a fee-on-transfer token, `minterAddress` receives `amount - fee`, but the event records `amount`.

The legacy `ERC20DepositHelper.sol` has the identical pattern: [2](#0-1) 

**Step 2 – The IC minter mints the full event value.**

In `rs/ethereum/cketh/minter/src/deposit.rs`, the `mint()` function iterates over `events_to_mint`, reads `event.value()` (which is the `amount` field parsed from the Ethereum log), and calls `icrc1_transfer` with that value as the mint amount: [3](#0-2) 

There is no adjustment for any fee that the ERC20 contract may have deducted during `transferFrom`. The minter mints `amount` ckERC20 tokens even though it only holds `amount - erc20_fee` ERC20 tokens.

**Step 3 – The minter's internal ERC20 balance accounting is also inflated.**

`State::record_event_to_mint` calls `update_balance_upon_deposit` using the same uncorrected `event.value()`: [4](#0-3) 

So the minter's tracked `erc20_balances` diverges from the actual on-chain balance, compounding the accounting error.

---

### Impact Explanation

This is a **chain-fusion mint/burn/replay bug** — specifically an over-minting bug that breaks the 1:1 ERC20 ↔ ckERC20 peg.

- Every deposit of a fee-on-transfer ERC20 token mints more ckERC20 than the ERC20 collateral actually received.
- Accumulated over many deposits, the total ckERC20 supply exceeds the ERC20 tokens held by the minter's Ethereum address.
- When users later withdraw (burn ckERC20 to receive ERC20), the minter will eventually be unable to fulfill withdrawals because it holds less ERC20 than the outstanding ckERC20 supply demands — a classic undercollateralization / bank-run scenario.
- **USDT** (`0xdAC17F958D2ee523a2206206994597C13D831ec7`) is already a supported ckERC20 token: [5](#0-4) 

USDT has a built-in fee mechanism that is currently set to zero but can be enabled by the USDT owner at any time. If enabled, every ckUSDT deposit would over-mint, immediately breaking the peg.

---

### Likelihood Explanation

- **Reachable by any unprivileged user**: any Ethereum address can call `depositErc20` on the helper contract with any supported ERC20 token. No privileged role is required.
- **USDT fee risk is real and documented**: the original M-07 report explicitly names USDT as a token that can enable fees later. USDT is already a supported ckERC20 token on mainnet.
- **No on-chain or IC-side guard exists**: neither the helper contract nor the minter canister checks whether the actual received balance matches the emitted `amount`.

---

### Recommendation

1. **In the Solidity helper contracts**: measure the minter's ERC20 balance before and after `transferFrom`, and emit the *actual received amount* (the balance delta) rather than the input `amount` parameter. This is the standard pattern for handling fee-on-transfer tokens:

   ```solidity
   uint256 balanceBefore = erc20Token.balanceOf(minterAddress);
   erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
   uint256 actualReceived = erc20Token.balanceOf(minterAddress) - balanceBefore;
   emit ReceivedEthOrErc20(erc20Address, msg.sender, actualReceived, principal, subaccount);
   ```

2. **In the minter canister**: as a defense-in-depth measure, consider cross-checking the minter's on-chain ERC20 balance against the sum of `events_to_mint` values before minting, and alerting or halting if a discrepancy is detected.

3. **Token allowlist policy**: explicitly document and enforce that fee-on-transfer ERC20 tokens must not be added to the supported ckERC20 token list until the helper contracts are upgraded.

---

### Proof of Concept

1. USDT owner enables the USDT transfer fee (e.g., 1 basis point = 0.01%).
2. Alice calls `depositErc20(USDT_ADDRESS, 1_000_000 /* 1 USDT */, alice_principal, subaccount)` on the `DepositHelperWithSubaccount` contract.
3. The helper calls `USDT.safeTransferFrom(Alice, minterAddress, 1_000_000)`. USDT deducts a 100-unit fee; minter receives 999,900 units.
4. The helper emits `ReceivedEthOrErc20(USDT, Alice, 1_000_000, alice_principal, subaccount)` — the full `1_000_000`, not `999_900`.
5. The ckETH minter scrapes the log, reads `value = 1_000_000`, and calls `icrc1_transfer` on the ckUSDT ledger to mint `1_000_000` ckUSDT to Alice. [3](#0-2) 

6. Alice holds 1,000,000 ckUSDT but the minter only holds 999,900 USDT. The 100-unit shortfall accumulates with every deposit, eventually making the minter insolvent for withdrawals.

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

**File:** rs/ethereum/cketh/minter/src/state.rs (L191-210)
```rust
    fn record_event_to_mint(&mut self, event: &ReceivedEvent) {
        let event_source = event.source();
        assert!(
            !self.events_to_mint.contains_key(&event_source),
            "there must be no two different events with the same source"
        );
        assert!(!self.minted_events.contains_key(&event_source));
        assert!(!self.invalid_events.contains_key(&event_source));
        if let ReceivedEvent::Erc20(event) = event {
            assert!(
                self.ckerc20_tokens
                    .contains_alt(&event.erc20_contract_address),
                "BUG: unsupported ERC-20 contract address in event {event:?}"
            )
        }

        self.events_to_mint.insert(event_source, event.clone());

        self.update_balance_upon_deposit(event)
    }
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L37-38)
```text
|USDT
|https://etherscan.io/token/0xdAC17F958D2ee523a2206206994597C13D831ec7[0xdAC17F958D2ee523a2206206994597C13D831ec7]
```
