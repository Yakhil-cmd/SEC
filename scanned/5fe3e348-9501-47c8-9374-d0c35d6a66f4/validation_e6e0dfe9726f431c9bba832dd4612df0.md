### Title
ckERC20 Minter Blindly Trusts Event-Reported Deposit Amount for Fee-on-Transfer / Upgradeable ERC-20 Tokens - (File: `rs/ethereum/cketh/minter/src/deposit.rs`)

---

### Summary

The ckETH minter canister mints ckERC20 tokens based solely on the `amount` field emitted in the `ReceivedErc20` / `ReceivedEthOrErc20` Ethereum event log. It never verifies the actual ERC-20 balance change at the minter's Ethereum address. For any supported ERC-20 token that charges a fee on transfer (or is upgraded to do so), the minter mints more ckERC20 than the ERC-20 tokens it actually holds, breaking the 1:1 backing invariant and enabling theft.

---

### Finding Description

**Deposit flow:**

1. The user calls `depositErc20` on the helper smart contract.
2. The helper calls `safeTransferFrom(msg.sender, minterAddress, amount)` and then emits the event with the *input* `amount` — not the actual balance delta of `minterAddress`.

In `ERC20DepositHelper.sol`: [1](#0-0) 

In `DepositHelperWithSubaccount.sol`: [2](#0-1) 

3. The minter scrapes these logs. `ReceivedErc20LogParser::parse_log` and `ReceivedEthOrErc20LogParser::parse_log` extract `value` directly from the event's `data` field (the `amount` argument), with no cross-check against actual on-chain balance changes: [3](#0-2) [4](#0-3) 

4. The `mint()` function in `deposit.rs` mints exactly `event.value()` ckERC20 tokens to the user — the event-reported amount, not the actual received amount: [5](#0-4) 

There is no `eth_getBalance` call, no balance-before/after comparison, and no mechanism to pause or reject deposits from tokens whose semantics have changed.

Additionally, the minter has no automated mechanism to detect when a supported ERC-20 token (many of which are upgradeable proxies) has been upgraded, and no ability to pause interactions with such a token pending re-approval.

The currently supported tokens include several upgradeable proxy contracts:
- **USDC** (`0xa0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48`) — Circle's upgradeable proxy; has a fee mechanism that is currently set to 0 but can be enabled by the contract owner.
- **USDT** (`0xdAC17F958D2ee523a2206206994597C13D831ec7`) — Has a configurable transfer fee (currently 0).
- **EURC** (`0x1aBaEA1f7C830bD89Acc67eC4af516284b1bC33c`) — Circle's upgradeable proxy. [6](#0-5) 

---

### Impact Explanation

**Ledger conservation break / chain-fusion mint bug.**

If a supported ERC-20 token charges a fee on transfer (e.g., 1%), then for a deposit of `X` tokens:
- The minter's Ethereum address receives `X * 0.99` tokens.
- The event emits `amount = X`.
- The minter mints `X` ckERC20 tokens on the IC.

The ckERC20 total supply now exceeds the actual ERC-20 backing held by the minter. An attacker who repeats this can accumulate ckERC20 tokens that are not backed 1:1. When they (or other users) attempt to withdraw, the minter's ERC-20 balance is insufficient to honor all outstanding ckERC20 tokens, resulting in failed withdrawals or draining of other users' funds.

---

### Likelihood Explanation

**Medium.** The precondition is that a currently supported ERC-20 token either already has a non-zero transfer fee or is upgraded to introduce one. USDT's fee mechanism is already present in its contract and can be activated by Tether's admin without any governance vote on the IC. USDC and EURC are upgradeable proxies whose implementation can be swapped. The IC has no automated detection of such changes and no circuit-breaker. An attacker does not need any privileged role on the IC — they only need to deposit the affected token after the fee is activated.

---

### Recommendation

1. **Short term:** For each supported ERC-20 token, verify at deposit time that the actual balance increase of the minter's Ethereum address matches the event-reported `amount`. This requires the minter to query `eth_getBalance` (or the ERC-20 `balanceOf`) before and after the deposit transaction, or to read the ERC-20 `Transfer` event emitted by the token contract itself (which reflects the actual transferred amount) rather than the helper contract's `ReceivedErc20` event.

2. **Medium term:** Implement an automated mechanism to monitor supported ERC-20 token contracts for upgrades (e.g., by tracking the implementation address of proxy contracts). Upon detecting an upgrade, automatically pause deposits/withdrawals for that token until it has been re-approved by NNS governance.

3. **Long term:** Require that all newly added ckERC20 tokens are non-upgradeable or have their upgrade keys burned, and enforce this at the `add_ckerc20_token` endpoint. [7](#0-6) 

---

### Proof of Concept

1. USDT's contract owner calls `setParams(basisPointsRate=100, maximumFee=...)` on the USDT contract, enabling a 1% transfer fee.
2. Attacker calls `approve(ERC20DepositHelper, 1_000_000 USDT)` on the USDT contract.
3. Attacker calls `deposit(usdt_address, 1_000_000, principal)` on `CkErc20Deposit`.
4. `safeTransferFrom` transfers `1_000_000` USDT; USDT's fee logic burns 1%, so the minter receives `990_000` USDT. The event emits `amount = 1_000_000`.
5. The minter scrapes the log, reads `value = 1_000_000` from the event data, and mints `1_000_000` ckUSDT to the attacker.
6. Attacker now holds `1_000_000` ckUSDT backed by only `990_000` USDT.
7. Attacker calls `withdraw_erc20` for `1_000_000` ckUSDT. The minter attempts to send `1_000_000` USDT but only holds `990_000`, causing the withdrawal to fail or underpay other users.
8. Repeating this attack drains the minter's ERC-20 reserves, making it impossible for honest users to redeem their ckUSDT at par.

The root cause is in `ReceivedErc20LogParser::parse_log` at `rs/ethereum/cketh/minter/src/eth_logs/parser.rs` line 97 and the `mint()` call at `rs/ethereum/cketh/minter/src/deposit.rs` line 80, where `event.value()` is used without any on-chain balance verification. [8](#0-7) [9](#0-8)

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

**File:** rs/ethereum/cketh/minter/src/eth_logs/parser.rs (L127-160)
```rust
        let [value_bytes, subaccount_bytes] =
            parse_hex_into_32_byte_words(entry.data, event_source)?;
        let subaccount = LedgerSubaccount::from_bytes(subaccount_bytes);
        let EventSource {
            transaction_hash,
            log_index,
        } = event_source;

        if erc20_contract_address == Address::ZERO {
            let value = Wei::from_be_bytes(value_bytes);
            return Ok(ReceivedEthEvent {
                transaction_hash,
                block_number,
                log_index,
                from_address,
                value,
                principal,
                subaccount,
            }
            .into());
        }

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

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L73-82)
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
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L14-48)
```text
| ERC-20 token symbol | ERC-20 smart contract address

|EURC
|https://etherscan.io/token/0x1aBaEA1f7C830bD89Acc67eC4af516284b1bC33c[0x1aBaEA1f7C830bD89Acc67eC4af516284b1bC33c]

|LINK
|https://etherscan.io/token/0x514910771AF9Ca656af840dff83E8264EcF986CA[0x514910771AF9Ca656af840dff83E8264EcF986CA]

|OCT
|https://etherscan.io/token/0xF5cFBC74057C610c8EF151A439252680AC68c6DC[0xF5cFBC74057C610c8EF151A439252680AC68c6DC]

|PEPE
|https://etherscan.io/token/0x6982508145454Ce325dDbE47a25d4ec3d2311933[0x6982508145454Ce325dDbE47a25d4ec3d2311933]

|SHIB
|https://etherscan.io/token/0x95aD61b0a150d79219dCF64E1E6Cc01f0B64C4cE[0x95aD61b0a150d79219dCF64E1E6Cc01f0B64C4cE]

|UNI
|https://etherscan.io/token/0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984[0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984]

|USDC
|https://etherscan.io/token/0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48[0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48]

|USDT
|https://etherscan.io/token/0xdAC17F958D2ee523a2206206994597C13D831ec7[0xdAC17F958D2ee523a2206206994597C13D831ec7]

|WBTC
|https://etherscan.io/token/0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599[0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599]

|wstETH
|https://etherscan.io/token/0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0[0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0]

|XAUt
|https://etherscan.io/token/0x68749665FF8D2d112Fa859AA293F07A622782F38[0x68749665FF8D2d112Fa859AA293F07A622782F38]
|===
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L562-574)
```rust
#[update]
async fn add_ckerc20_token(erc20_token: AddCkErc20Token) {
    let orchestrator_id = read_state(|s| s.ledger_suite_orchestrator_id)
        .unwrap_or_else(|| ic_cdk::trap("ERROR: ERC-20 feature is not activated"));
    if orchestrator_id != ic_cdk::api::msg_caller() {
        ic_cdk::trap(format!(
            "ERROR: only the orchestrator {orchestrator_id} can add ERC-20 tokens"
        ));
    }
    let ckerc20_token = erc20::CkErc20Token::try_from(erc20_token)
        .unwrap_or_else(|e| ic_cdk::trap(format!("ERROR: {e}")));
    mutate_state(|s| process_event(s, EventType::AddedCkErc20Token(ckerc20_token)));
}
```
